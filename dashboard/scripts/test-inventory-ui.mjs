// Inventory UI shell and primitives (Documentation/Inventory/04-ROADMAP task 1.3, tests T-UI1 and T-UI3):
//   (a) every navigation.ts `to` target resolves to a route declared in App.tsx (the whole tree),
//   (b) the asset filter vocabulary: round trips, allowed values, rejection of unknown values,
//   (c) DrillLink / assetsHref URL building and the drill-count contract,
//   (d) the primitives' pure logic: freshness, version ordering, bucket collapsing, facets,
//   (e) the components themselves, rendered to markup: badges, bars, facets, meta-line, access gate, layout,
//   (f) the URL-state rules (what is read, what is written, push versus replace).
// Run from dashboard/: node scripts/test-inventory-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')

/* ── bundle the code under test (the app's own alias, real components) ─────────────────────── */

const entry = `
export * as filters from '@/pages/inventory/filters'
export * as urlState from '@/pages/inventory/urlState'
export * as model from '@/components/inventory/model'
export * as kb from '@/components/inventory/InventoryKbLink'
export * as facet from '@/components/inventory/FacetPanel'
export * as access from '@/components/inventory/InventoryAccess'
export * as nav from '@/components/layout/navigation'
export * as navAccess from '@/components/layout/navAccess'
export { DrillLink } from '@/components/inventory/DrillLink'
export { FreshnessBadge } from '@/components/inventory/FreshnessBadge'
export { SourceBadge } from '@/components/inventory/SourceBadge'
export { VersionDistributionBar } from '@/components/inventory/VersionDistributionBar'
export { FacetPanel } from '@/components/inventory/FacetPanel'
export { InventoryMetaLine } from '@/components/inventory/InventoryMetaLine'
export { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
export { InventoryAccessGate } from '@/components/inventory/InventoryAccess'
export { InventoryLayout, inventoryTabs, isProductPath } from '@/pages/inventory/InventoryLayout'
export { InventorySettingsPage } from '@/pages/inventory/InventorySettingsPage'
export { useInventoryUrlState } from '@/pages/inventory/useInventoryUrlState'
export * as ov from '@/components/inventory/overviewModel'
export * as am from '@/components/inventory/assetModel'
export * as cm from '@/components/inventory/changeModel'
export { InventoryKpiCard } from '@/components/inventory/InventoryKpiCard'
export { Donut, sliceAngles, arcPath } from '@/components/inventory/Donut'
export { Sparkline } from '@/components/inventory/Sparkline'
export { AssetTable } from '@/components/inventory/AssetTable'
export { AssetToolbar } from '@/components/inventory/AssetToolbar'
export { FlyoutBody, FlyoutHeader } from '@/components/inventory/AssetFlyout'
export { InventoryOverviewPage } from '@/pages/inventory/InventoryOverviewPage'
export { InventoryAssetsPage } from '@/pages/inventory/InventoryAssetsPage'
export { useAuth } from '@/stores/auth'
export * as authStore from '@/stores/auth'
export * as chg from '@/components/inventory/changesModel'
export * as osm from '@/components/inventory/osTreeModel'
export * as search from '@/components/inventory/searchBoxModel'
export * as invApi from '@/pages/inventory/api'
export * as chunk from '@/lib/chunkRecovery'
export { ChunkLoadNotice } from '@/components/ChunkLoadNotice'
export { ErrorBoundary } from '@/components/ErrorBoundary'
export * as banner from '@/components/inventory/DataStatusBanner'
export * as invMeta from '@/pages/inventory/useInventoryMeta'
export * as chipFocus from '@/components/inventory/useChipFocus'
export { FilterChipList } from '@/components/inventory/FilterChipList'
export { pageBoundaryKey } from '@/pages/inventory/InventoryLayout'
export { AgentCoverageLine } from '@/components/inventory/OverviewPanels'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter, Routes, Route, Outlet } from 'react-router-dom'
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
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { filters: F, urlState: U, model: M, kb: KB, facet: FP, access: AX, nav: NAV, navAccess: NA, ov: OV, am: AM, cm: CM, authStore: AUTH, search: SEARCH, invApi: API, chg: CHG, chunk: CHUNK, banner: BANNER, invMeta: META, chipFocus: CHIPFOCUS } = m
const h = m.createElement

/* ═══ (a) every navigation target resolves to a declared route ═══════════════════════════════ */

/** The routes App.tsx declares, as full path patterns, read from the JSX itself (not by regex). */
function declaredRoutes(source) {
  const sf = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const patterns = []
  const attrs = (opening) => {
    const out = {}
    for (const a of opening.attributes.properties) {
      if (!ts.isJsxAttribute(a)) continue
      const name = a.name.getText()
      out[name] = a.initializer && ts.isStringLiteral(a.initializer) ? a.initializer.text : true
    }
    return out
  }
  const join = (prefix, p) => (p.startsWith('/') ? p : prefix === '/' ? `/${p}` : `${prefix}/${p}`)
  const routeChildren = (node) => (ts.isJsxElement(node) ? node.children.filter((c) => (ts.isJsxElement(c) || ts.isJsxSelfClosingElement(c)) && (ts.isJsxElement(c) ? c.openingElement : c).tagName.getText() === 'Route') : [])
  function walk(node, prefix) {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      const opening = ts.isJsxElement(node) ? node.openingElement : node
      if (opening.tagName.getText() === 'Route') {
        const a = attrs(opening)
        const full = typeof a.path === 'string' ? join(prefix, a.path) : prefix
        const kids = routeChildren(node)
        // A layout route on its own renders an empty outlet: only a leaf or an index child is a page.
        if (a.index === true || kids.length === 0) patterns.push(full)
        for (const k of kids) walk(k, full)
        // Routes nested inside non-Route wrappers (none today) would be found by the generic walk below.
        return
      }
    }
    ts.forEachChild(node, (c) => walk(c, prefix))
  }
  walk(sf, '/')
  return [...new Set(patterns)]
}

const patternRegex = (pattern) =>
  new RegExp(`^${pattern.split('/').map((seg) => (seg === '*' ? '.*' : seg.startsWith(':') ? '[^/]+' : seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).join('/')}/?$`)

const routes = declaredRoutes(read('src/App.tsx')).filter((p) => !p.endsWith('*'))
const routeMatchers = routes.map((p) => ({ pattern: p, re: patternRegex(p) }))
const resolves = (to) => {
  const pathname = to.split('#')[0].split('?')[0] || '/'
  return routeMatchers.some((r) => r.re.test(pathname))
}
const ownsPrefix = (prefix) => routes.some((p) => p === prefix.replace(/\/$/, '') || p.startsWith(prefix.endsWith('/') ? prefix : `${prefix}/`))

assert(routes.includes('/inventory') && routes.includes('/inventory/assets') && routes.includes('/inventory/software/:productKey'), 'the route reader finds the inventory routes')
assert(routes.includes('/netflow/:section') && routes.includes('/reports') && routes.includes('/udt/endpoints/:id'), 'the route reader follows nested and path-less layout routes')

{
  const broken = []
  const nodes = (groups) => groups.flatMap((g) => g.items.flatMap((i) => [{ g, n: i }, ...(i.children ?? []).map((c) => ({ g, n: c, parent: i }))]))
  for (const { g, n } of nodes(NAV.NAV_GROUPS)) {
    if (!resolves(n.to)) broken.push(`${g.id}: ${n.label} -> ${n.to} (no route in App.tsx)`)
    for (const prefix of n.extra ?? []) {
      if (!ownsPrefix(prefix)) broken.push(`${g.id}: ${n.label} owns ${prefix}, but App.tsx declares nothing below it`)
    }
  }
  assert.deepEqual(broken, [], `navigation targets without a declared route:\n  ${broken.join('\n  ')}`)
  const count = nodes(NAV.NAV_GROUPS).length
  assert(count > 80, `walked the whole navigation tree (${count} entries)`)
}

// The module's own rows: each resolves, the product page resolves under Software, and nothing is orphaned.
for (const to of ['/inventory', '/inventory/assets', '/inventory/os', '/inventory/software', '/inventory/hardware', '/inventory/changes', '/inventory/settings']) {
  assert(resolves(to), `${to} resolves`)
}
assert(resolves('/inventory/software/google-chrome') && !resolves('/inventory/nope'))
{
  const inventoryRoutes = routes.filter((p) => p === '/inventory' || p.startsWith('/inventory/')).sort()
  assert.deepEqual(inventoryRoutes, [
    '/inventory', '/inventory/assets', '/inventory/changes', '/inventory/hardware', '/inventory/os',
    '/inventory/settings', '/inventory/software', '/inventory/software/:productKey',
  ], 'Phase 1 ships exactly these Inventory routes (no Lifecycle, no Compliance)')
  // The smoke script must require every one of them.
  const smoke = read('scripts/smoke-routes.mjs')
  for (const r of inventoryRoutes) assert(smoke.includes(`'${r}'`), `smoke-routes.mjs requires ${r}`)
}
// A deliberately broken tree is caught (the check is not vacuous).
assert(!resolves('/devices-typo') && !resolves('/inventory/lifecycle') && !resolves('/inventory/software/a/b') && resolves('/servers/inventory'))

/* ═══ (b) the filter vocabulary ═════════════════════════════════════════════════════════════ */

const KEYS = [
  'q', 'kind', 'os_family', 'os_product', 'os_release', 'os_version', 'vendor', 'model', 'site', 'tag', 'source', 'freshness', 'health', 'product', 'product_version',
  // Server hardware and security drill-downs the API also accepts (inventory_query.AssetFilters).
  'cpu_model', 'memory', 'disks', 'virtualization', 'security_agent',
  // ENTITY-MIB component drill-downs of the Hardware page (network devices only).
  'module_class', 'module_model',
]
assert.deepEqual([...F.ASSET_FILTER_KEYS], KEYS, 'the vocabulary is exactly the pinned keys, in URL order')
assert.deepEqual([...F.ASSET_KINDS], ['server', 'device'])
assert.deepEqual([...F.FRESHNESS_VALUES], ['fresh', 'aging', 'stale', 'never', 'not_applicable', 'stale_or_never'])
assert.deepEqual([...F.HEALTH_PREDICATES], ['missing_os_version', 'missing_model', 'missing_serial', 'unknown_vendor', 'stale', 'never', 'software_truncated', 'missing_security_agent'])
assert.deepEqual([...F.MEMORY_BUCKETS], ['le8', 'le16', 'le32', 'le64', 'le128', 'gt128', 'unknown'])
assert.deepEqual([...F.DISK_BUCKETS], ['1', '2', '3-4', '5-8', 'ge9', 'unknown'])
assert.deepEqual([...F.SECURITY_AGENT_VALUES], ['present', 'missing', 'unknown'])
assert.deepEqual([...F.MODULE_CLASSES], ['chassis', 'backplane', 'module', 'powerSupply', 'fan', 'cpu', 'stack'])
assert.equal(F.MAX_PAGE_SIZE, 500)

const sp = (q) => new URLSearchParams(q)
const full = {
  q: 'forti gate', kind: 'device', os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2', os_version: '7.2.5',
  vendor: 'Fortinet', model: 'FortiGate-200F', site: 'DC 1', tag: ['dc-east', 'pci'], source: 'snmp',
  freshness: 'stale_or_never', health: 'missing_serial', product: 'google-chrome', product_version: '152.0.8',
  cpu_model: 'Intel Xeon Gold 6338', memory: 'le64', disks: '3-4', virtualization: 'vmware', security_agent: 'missing',
  module_class: 'powerSupply', module_model: 'PWR-C1-715WAC',
}
// Round trip: write then read gives the same object, and writing again gives the same string.
{
  const qs = F.writeAssetFilters(full).toString()
  assert.equal(qs, 'q=forti+gate&kind=device&os_family=fortios&os_product=FortiOS&os_release=7.2&os_version=7.2.5&vendor=Fortinet&model=FortiGate-200F&site=DC+1&tag=dc-east&tag=pci&source=snmp&freshness=stale_or_never&health=missing_serial&product=google-chrome&product_version=152.0.8&cpu_model=Intel+Xeon+Gold+6338&memory=le64&disks=3-4&virtualization=vmware&security_agent=missing&module_class=powerSupply&module_model=PWR-C1-715WAC')
  const back = F.parseAssetFilters(qs)
  assert.deepEqual(back.filters, full)
  assert.deepEqual(back.rejected, [])
  assert.equal(F.writeAssetFilters(back.filters).toString(), qs)
  // The order of the incoming parameters does not change the canonical output.
  const shuffled = [...new URLSearchParams(qs)].reverse()
  assert.equal(F.writeAssetFilters(F.parseAssetFilters(new URLSearchParams(shuffled)).filters).toString(), qs)
  // A leading question mark and a plain string are fine.
  assert.deepEqual(F.parseAssetFilters(`?${qs}`).filters, full)
}
// Every key alone round-trips too.
for (const key of KEYS) {
  const one = key === 'product_version' ? { product: full.product, product_version: full.product_version } : { [key]: full[key] }
  assert.deepEqual(F.parseAssetFilters(F.writeAssetFilters(one)).filters, one, key)
}
assert.deepEqual(F.parseAssetFilters('').filters, {})
assert(F.isAssetFiltersEmpty({}) && !F.isAssetFiltersEmpty({ tag: ['a'] }) && F.isAssetFiltersEmpty({ tag: [] }))

// Unknown values are rejected, reported, and never passed on.
{
  const bad = F.parseAssetFilters('kind=router&freshness=recent&health=orphaned&os_family=Not%20A%20Slug&source=a%2Fb&tag=&memory=64gb&disks=4&security_agent=none')
  assert.deepEqual(bad.filters, {})
  assert.deepEqual(bad.rejected.map((r) => r.key).sort(), ['disks', 'freshness', 'health', 'kind', 'memory', 'os_family', 'security_agent', 'source'].sort(), 'a blank tag is no tag, not an error')
  for (const r of bad.rejected) assert(r.reason && typeof r.reason === 'string', r.key)
  // Enum values are case-insensitive on the way in and canonical on the way out.
  assert.deepEqual(F.parseAssetFilters('kind=SERVER&freshness=Stale&health=MISSING_SERIAL&os_family=Windows').filters, { kind: 'server', freshness: 'stale', health: 'missing_serial', os_family: 'windows' })
  // Keys that are not in the vocabulary are ignored (they belong to the page: sort, page, tab, asset ...).
  assert.deepEqual(F.parseAssetFilters('sort=name&page=2&tab=versions&asset=device:1&colour=red&vendor=Cisco').filters, { vendor: 'Cisco' })
  // Over-long and control characters.
  assert.equal(F.parseAssetFilters(`q=${'x'.repeat(201)}`).rejected[0].key, 'q')
  assert.equal(F.parseAssetFilters(`q=${'x'.repeat(200)}`).rejected.length, 0)
  assert.equal(F.parseAssetFilters('q=a%00b').rejected[0].reason, 'contains control characters')
  assert.equal(F.parseAssetFilters('q=%20%20').filters.q, undefined, 'blank text is no filter')
  assert.equal(F.parseAssetFilters('q=a%09%20%20b').filters.q, 'a b', 'whitespace collapses')
}
// Unknown is addressed, not typed. The identity fields share one sentinel (`unknown`, which the API reads as
// "missing"); serial has none, so its unknown is the health predicate.
{
  for (const key of ['os_family', 'os_product', 'os_release', 'os_version', 'vendor', 'model', 'cpu_model', 'memory', 'disks', 'virtualization', 'security_agent', 'module_model']) {
    assert.deepEqual(F.parseAssetFilters(`${key}=unknown`).filters, { [key]: 'unknown' }, `${key}=unknown is the sentinel`)
  }
  assert.equal(F.filterValueLabel('cpu_model', 'unknown'), 'Unknown')
  assert.equal(F.filterValueLabel('memory', 'le64'), '33 to 64 GB')
  assert.equal(F.filterValueLabel('disks', '3-4'), '3 to 4 disks')
  assert.equal(F.filterValueLabel('security_agent', 'missing'), 'Missing')
  for (const key of ['vendor', 'model']) {
    assert.equal(F.filterValueLabel(key, 'unknown'), 'Unknown', `${key}=unknown reads as the word Unknown`)
    assert.equal(F.filterValueLabel(key, 'UNKNOWN'), 'Unknown')
  }
  assert.deepEqual(F.UNKNOWN_FILTER.os_product, { os_product: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.os_release, { os_release: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.vendor, { vendor: 'unknown' }, 'the Hardware page\'s unknown rows drill to the sentinel, which counts by-design assets too')
  assert.deepEqual(F.UNKNOWN_FILTER.model, { model: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.serial, { health: 'missing_serial' })
  assert.deepEqual(F.UNKNOWN_FILTER.os_version, { os_version: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.os_family, { os_family: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.cpu_model, { cpu_model: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.memory, { memory: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.disks, { disks: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.virtualization, { virtualization: 'unknown' })
  assert.deepEqual(F.UNKNOWN_FILTER.module_model, { module_model: 'unknown' })
  for (const patch of Object.values(F.UNKNOWN_FILTER)) assert.deepEqual(F.normaliseAssetFilters(patch).rejected, [], 'every unknown address is itself valid')
}
// ENTITY-MIB component filters: the class keeps the API's spelling (`powerSupply`) whatever case came in.
{
  for (const c of F.MODULE_CLASSES) assert.deepEqual(F.parseAssetFilters(`module_class=${c}`).filters, { module_class: c }, c)
  assert.deepEqual(F.parseAssetFilters('module_class=POWERSUPPLY').filters, { module_class: 'powerSupply' }, 'case-insensitive in, canonical out')
  assert.deepEqual(F.parseAssetFilters('module_class=powersupply&module_model=C9300-NM-8X').filters, { module_class: 'powerSupply', module_model: 'C9300-NM-8X' })
  const bad = F.parseAssetFilters('module_class=transceiver&module_model=%20')
  assert.deepEqual(bad.filters, {})
  assert.deepEqual(bad.rejected.map((r) => r.key), ['module_class', 'module_model'], 'an unknown class and a blank model are rejected, never passed on')
  assert.equal(F.writeAssetFilters({ module_class: 'fan', module_model: 'unknown' }).toString(), 'module_class=fan&module_model=unknown')
  assert.equal(F.writeAssetFilters({ module_class: 'bogus' }).toString(), '', 'writing validates like reading')
  assert.equal(F.assetsHref({ module_class: 'powerSupply', module_model: 'unknown' }), '/inventory/assets?module_class=powerSupply&module_model=unknown')
  assert.equal(F.filterValueLabel('module_class', 'powerSupply'), 'Power supply')
  assert.equal(F.filterValueLabel('module_model', 'unknown'), 'Unknown')
  assert.equal(F.filterValueLabel('module_model', 'WS-X6748'), 'WS-X6748')
  assert.deepEqual(F.activeFilterChips({ module_class: 'fan', module_model: 'unknown' }).map((c) => `${c.label}: ${c.text}`), ['Component class: Fan', 'Component model: Unknown'])
  for (const c of F.MODULE_CLASSES) assert(F.MODULE_CLASS_LABELS[c], `label for ${c}`)
  // The pair composes with the other asset filters (a module group on a vendor's device list).
  assert.deepEqual(F.parseAssetFilters(F.writeAssetFilters({ kind: 'device', vendor: 'Cisco', module_class: 'chassis', module_model: 'C9300-48P' })).filters, { kind: 'device', vendor: 'Cisco', module_class: 'chassis', module_model: 'C9300-48P' })
}
// Tags: repeatable, de-duplicated case-insensitively, bounded.
{
  assert.deepEqual(F.parseAssetFilters('tag=PCI&tag=dc-east&tag=pci').filters.tag, ['dc-east', 'PCI'], 'a set: de-duplicated ignoring case, in a canonical order')
  assert.equal(F.parseAssetFilters(`tag=${'t'.repeat(65)}`).rejected[0].key, 'tag')
  const many = Array.from({ length: 22 }, (_, i) => `tag=t${i}`).join('&')
  assert.equal(F.parseAssetFilters(many).filters.tag.length, 20)
  assert.equal(F.parseAssetFilters(many).rejected.length, 2)
  assert.deepEqual(F.toggleTag({ tag: ['pci'] }, 'PCI'), {}, 'toggling an existing tag removes it, whatever its case')
  assert.deepEqual(F.toggleTag({}, 'pci'), { tag: ['pci'] })
  assert.deepEqual(F.toggleTag({ tag: ['pci'] }, 'dc-east'), { tag: ['dc-east', 'pci'] })
}
// product_version only goes with a product.
{
  const r = F.parseAssetFilters('product_version=1.0')
  assert.deepEqual(r.filters, {})
  assert.equal(r.rejected[0].reason, 'needs product')
  assert.deepEqual(F.parseAssetFilters('product=7-zip&product_version=25.01').filters, { product: '7-zip', product_version: '25.01' })
  assert.equal(F.writeAssetFilters({ product_version: '1.0' }).toString(), '', 'writing validates like reading')
}
// Writing validates too: a typed object cannot smuggle a bad value into a URL.
assert.equal(F.writeAssetFilters({ kind: 'router', vendor: 'Cisco', health: 'orphaned' }).toString(), 'vendor=Cisco')
// A write replaces the vocabulary keys it finds and leaves every other parameter alone.
assert.equal(F.writeAssetFilters({ vendor: 'Cisco' }, sp('vendor=Fortinet&kind=server&page=3&sort=ip')).toString(), 'page=3&sort=ip&vendor=Cisco')
// Patches: null, undefined, '' and [] remove; a bad value is dropped.
{
  const cur = { vendor: 'Cisco', kind: 'device', tag: ['a'] }
  assert.deepEqual(F.patchAssetFilters(cur, { vendor: null }), { kind: 'device', tag: ['a'] })
  assert.deepEqual(F.patchAssetFilters(cur, { tag: [] , kind: undefined }), { vendor: 'Cisco' })
  assert.deepEqual(F.patchAssetFilters(cur, { kind: 'router' }), { vendor: 'Cisco', tag: ['a'] })
  assert.deepEqual(F.patchAssetFilters(cur, { model: 'C9300', vendor: '' }), { kind: 'device', tag: ['a'], model: 'C9300' })
}
// Chips and labels.
{
  const chips = F.activeFilterChips({ kind: 'server', health: 'missing_security_agent', tag: ['a', 'b'], os_family: 'unknown' })
  assert.deepEqual(chips.map((c) => `${c.label}: ${c.text}`), ['Kind: Server', 'OS family: Unknown', 'Tag: a', 'Tag: b', 'Health: No endpoint protection'])
  for (const k of KEYS) assert(F.FILTER_LABELS[k], `label for ${k}`)
  for (const v of F.HEALTH_PREDICATES) assert(F.HEALTH_LABELS[v], `label for ${v}`)
  for (const v of F.FRESHNESS_VALUES) assert(F.FRESHNESS_LABELS[v], `label for ${v}`)
}

// Paging and ordering.
{
  assert.deepEqual(F.parseListState('sort=ip&order=DESC&page=3&page_size=100'), { sort: 'ip', order: 'desc', page: 3, page_size: 100 })
  assert.deepEqual(F.parseListState('sort=colour&order=up&page=0&page_size=-5'), {}, 'invalid values are simply absent')
  assert.deepEqual(F.parseListState('page=abc&page_size=1.5'), {})
  assert.equal(F.parseListState('page_size=9999').page_size, 500, 'page_size is capped at the API maximum')
  assert.equal(F.parseListState('page=99999999999').page, undefined, 'absurd numbers are not pages')
  assert.equal(F.parseListState('page=500000').page, 100000, 'page is capped at the API maximum')
  assert.equal(F.MAX_PAGE, 100000)
  assert.equal(F.parseListState('sort=freshness').sort, 'freshness')
  assert.equal(F.parseListState('sort=os_version&order=desc').sort, 'os_version')
  assert.deepEqual(F.nextSort({ sort: 'name', order: 'asc' }, 'name'), { sort: 'name', order: 'desc' })
  assert.deepEqual(F.nextSort({ sort: 'name', order: 'desc' }, 'ip'), { sort: 'ip', order: 'asc' })
  // The address bar leaves defaults out; the API request is always explicit.
  assert.equal(F.assetQueryToParams({ vendor: 'Cisco' }).toString(), 'vendor=Cisco')
  assert.equal(F.assetQueryToParams({ vendor: 'Cisco', sort: 'name', order: 'asc', page: 1, page_size: 50 }).toString(), 'vendor=Cisco')
  assert.equal(F.assetQueryToParams({ vendor: 'Cisco', sort: 'ip', order: 'desc', page: 2, page_size: 100 }).toString(), 'vendor=Cisco&sort=ip&order=desc&page=2&page_size=100')
  assert.equal(F.assetQueryToParams({ vendor: 'Cisco' }, { explicit: true }).toString(), 'vendor=Cisco&sort=name&order=asc&page=1&page_size=50')
  assert.equal(F.assetQueryToParams({ page_size: 9999 }, { explicit: true }).get('page_size'), '500')
}

// An asset is addressed as <kind>:<uuid>.
{
  const id = '3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
  assert.equal(F.assetAddress('device', id), `device:${id}`)
  assert.deepEqual(F.parseAssetAddress(`device:${id}`), { kind: 'device', id })
  assert.deepEqual(F.parseAssetAddress(`SERVER:${id.toUpperCase()}`), { kind: 'server', id })
  for (const bad of ['', null, undefined, id, `endpoint:${id}`, `device:${id}x`, 'device:123', `device:${id}/../x`]) assert.equal(F.parseAssetAddress(bad), null, String(bad))
}

/* ═══ (c) DrillLink and URL building ════════════════════════════════════════════════════════ */

assert.equal(F.assetsHref({}), '/inventory/assets')
assert.equal(F.assetsHref({ os_family: 'windows', os_release: '2019' }), '/inventory/assets?os_family=windows&os_release=2019')
assert.equal(F.assetsHref({ freshness: 'stale' }), '/inventory/assets?freshness=stale')
assert.equal(F.assetsHref({ product: 'google-chrome', product_version: '152.0.8' }), '/inventory/assets?product=google-chrome&product_version=152.0.8')
assert.equal(F.assetsHref({ health: 'unknown_vendor', kind: 'device' }, { sort: 'ip', order: 'desc' }), '/inventory/assets?kind=device&health=unknown_vendor&sort=ip&order=desc')
assert.equal(F.assetsHref({ vendor: 'Palo Alto Networks', model: 'PA-3220' }), '/inventory/assets?vendor=Palo+Alto+Networks&model=PA-3220')
assert.equal(F.assetsHref({ vendor: 'R&D <b>"x"</b>' }), '/inventory/assets?vendor=R%26D+%3Cb%3E%22x%22%3C%2Fb%3E', 'values are encoded, never spliced into the URL')
{
  const id = '3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
  assert.equal(F.assetsHref({}, { asset: `device:${id}` }), `/inventory/assets?asset=device%3A${id}`)
  assert.equal(F.assetsHref({ kind: 'device' }, { asset: 'device:not-a-uuid' }), '/inventory/assets?kind=device', 'a malformed asset address is dropped')
}
// The URL and the API share one vocabulary: the same keys, in the same order, with the same values.
{
  const f = { kind: 'server', os_family: 'windows', tag: ['pci'], freshness: 'stale', health: 'missing_os_version' }
  const url = new URL(F.assetsHref(f), 'http://x')
  const api = F.assetQueryToParams(f, { explicit: true })
  for (const key of ['kind', 'os_family', 'tag', 'freshness', 'health']) assert.deepEqual(url.searchParams.getAll(key), api.getAll(key), key)
  assert.deepEqual([...url.searchParams.keys()].filter((k) => KEYS.includes(k)), [...api.keys()].filter((k) => KEYS.includes(k)))
}

const render = (el, url = '/') => m.renderToStaticMarkup(h(m.MemoryRouter, { initialEntries: [url] }, el))
const textOf = (html) => decode(html.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ')
const decode = (s) => s.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#x27;/g, "'")
/** Every <a> in the markup: its href (decoded) and its attributes. */
const anchors = (html) => [...html.matchAll(/<a\s([^>]*)>/g)].map((a) => {
  const attrs = Object.fromEntries([...a[1].matchAll(/([\w:-]+)="([^"]*)"/g)].map((x) => [x[1], decode(x[2])]))
  return { href: attrs.href, attrs }
})

{
  const html = render(h(m.DrillLink, { filters: { os_family: 'windows', os_release: '2019' }, count: 23, title: 'Windows Server 2019' }, '23'))
  const [a] = anchors(html)
  assert.equal(a.href, '/inventory/assets?os_family=windows&os_release=2019')
  assert.equal(a.attrs['data-drill-count'], '23', 'the number the link stands for is on the element')
  assert.equal(a.attrs.title, 'Windows Server 2019')
  assert(html.includes('>23</a>'))
  assert.equal(anchors(render(h(m.DrillLink, { filters: { health: 'stale' } }, 'x')))[0].attrs['data-drill-count'], undefined, 'no count, no attribute')
  assert.equal(anchors(render(h(m.DrillLink, { filters: {}, list: { sort: 'last_seen', order: 'desc' } }, 'all')))[0].href, '/inventory/assets?sort=last_seen&order=desc')
}
// The compile-time guard is still in place (tsc in `npm run build` runs it).
{
  const typecheck = read('src/pages/inventory/vocabulary.typecheck.tsx')
  assert((typecheck.match(/@ts-expect-error/g) ?? []).length >= 10, 'vocabulary.typecheck.tsx keeps its negative type tests')
}

/* ═══ (d) pure logic of the primitives ═══════════════════════════════════════════════════════ */

// Numbers: exact, grouped, missing is a dash.
assert.equal(M.fmtInt(1840), '1,840')
assert.equal(M.fmtInt(39120), '39,120', 'never abbreviated: the drilled list total must read the same')
assert.equal(M.fmtInt(0), '0')
assert.equal(M.fmtInt(null), '—')
assert.equal(M.fmtInt(undefined), '—')
assert.equal(M.fmtInt(Number.NaN), '—')
assert.equal(M.fmtPct(94.44), '94.4%')
assert.equal(M.fmtPct(100), '100.0%')
assert.equal(M.fmtPct(null), '—')
assert.equal(M.share(1, 4), 25)
assert.equal(M.share(1, 0), 0)
assert.equal(M.plural(1, 'host'), '1 host')
assert.equal(M.plural(1840, 'host'), '1,840 hosts')

// Freshness classification, at the boundaries.
{
  const now = Date.parse('2026-10-08T12:00:00Z')
  const ago = (ms) => new Date(now - ms).toISOString()
  const H = 3_600_000
  const D = 24 * H
  assert.equal(M.classifyFreshness('server', ago(23 * H), now), 'fresh')
  assert.equal(M.classifyFreshness('server', ago(24 * H), now), 'fresh', 'exactly at the target is still fresh')
  assert.equal(M.classifyFreshness('server', ago(24 * H + 1000), now), 'aging')
  assert.equal(M.classifyFreshness('device', ago(2 * H), now), 'fresh')
  assert.equal(M.classifyFreshness('device', ago(3 * H), now), 'aging', 'devices have a tighter target than servers')
  assert.equal(M.classifyFreshness('server', ago(7 * D), now), 'aging')
  assert.equal(M.classifyFreshness('server', ago(7 * D + 1000), now), 'stale')
  assert.equal(M.classifyFreshness('server', null, now), 'never')
  assert.equal(M.classifyFreshness('device', undefined, now), 'never')
  assert.equal(M.classifyFreshness('server', ago(-5 * 60_000), now), 'fresh', 'a timestamp slightly in the future (clock skew) is fresh')
  assert.equal(M.classifyFreshness('server', ago(30 * H), now, { fresh_hours: { server: 48, device: 2 }, stale_days: 7 }), 'fresh', 'thresholds come from settings')
  assert.equal(M.classifyFreshness('server', '2026-10-08 11:00:00', now), 'fresh', 'a naive timestamp is read as UTC')
  // Age in words (the wording of lib/utils relativeTime).
  assert.equal(M.ageWords(ago(3000), now), 'just now')
  assert.equal(M.ageWords(ago(42_000), now), '42s ago')
  assert.equal(M.ageWords(ago(5 * 60_000), now), '5m ago')
  assert.equal(M.ageWords(ago(6 * H), now), '6h ago')
  assert.equal(M.ageWords(ago(3 * D), now), '3d ago')
  assert.equal(M.ageWords(null, now), 'never')
  assert.equal(M.ageWords('garbage', now), '—')
  // The tooltip separates reachability from data.
  const title = M.freshnessTitle('aging', ago(6 * H), ago(60_000), now)
  assert.match(title, /Last inventory \(data\): 6h ago/)
  assert.match(title, /Last seen \(reachability\): 1m ago/)
  assert.match(M.freshnessTitle('never', null, null, now), /Last inventory \(data\): never\. Last seen \(reachability\): never/)
  for (const s of M.FRESHNESS_STATES) {
    const meta = M.FRESHNESS_META[s]
    assert(meta.label && meta.meaning && meta.tone && meta.dot, s)
  }
  assert.match(M.FRESHNESS_META.fresh.tone, /success-fg/)
  assert.match(M.FRESHNESS_META.aging.tone, /warning-fg/)
  assert.match(M.FRESHNESS_META.stale.tone, /danger-fg/)
  assert.match(M.FRESHNESS_META.never.tone, /text-text2/, 'Never keeps AA contrast on its own fill (muted on surface2 is 4.3:1)')
  // As-of label: time today, date and time otherwise.
  assert.equal(M.asOfLabel('2026-10-08T09:05:00Z', now), '09:05')
  assert.equal(M.asOfLabel('2026-10-07T14:05:00Z', now), '2026-10-07 14:05')
  assert.equal(M.asOfLabel(null, now), 'unknown')
}

// Sources.
assert.equal(M.sourceLabel('agent'), 'Agent')
assert.equal(M.sourceLabel('snmp'), 'SNMP')
assert.equal(M.sourceLabel('template'), 'Template')
assert.equal(M.sourceLabel('entity'), 'ENTITY-MIB')
assert.equal(M.sourceLabel('winrm'), 'WinRM')
assert.equal(M.sourceLabel('ssh'), 'SSH')
assert.equal(M.sourceLabel('wmi'), 'WMI')
assert.equal(M.sourceLabel('controller'), 'Controller')
assert.equal(M.sourceLabel('icmp'), 'ICMP')
assert.equal(M.sourceLabel('SNMP'), 'SNMP', 'case-insensitive')
assert.equal(M.sourceLabel('carrier-pigeon'), 'Carrier-pigeon', 'an unlisted source shows as itself')
assert.equal(M.sourceLabel(null), 'Unknown source')

// Version ordering.
{
  const asc = (list) => [...list].sort(M.naturalVersionCompare)
  assert.deepEqual(asc(['10.0.20348.2700', '10.0.20348.587', '10.0.20348.60']), ['10.0.20348.60', '10.0.20348.587', '10.0.20348.2700'])
  assert.deepEqual(asc(['11.1.2-h10', '11.1.2-h3', '11.1.2']), ['11.1.2', '11.1.2-h3', '11.1.2-h10'])
  assert.deepEqual(asc(['17.9.4a', '17.9.4', '17.9.10']), ['17.9.4', '17.9.4a', '17.9.10'])
  assert.deepEqual(asc(['15.2(4)E10', '15.2(4)E8']), ['15.2(4)E8', '15.2(4)E10'])
  assert.deepEqual(asc(['7.2.8', '7.4.4', '7.2.5']), ['7.2.5', '7.2.8', '7.4.4'])
  assert.deepEqual(asc(['21.4R3-S5.4', '21.4R3-S5']), ['21.4R3-S5', '21.4R3-S5.4'])
  assert.equal(M.naturalVersionCompare('2.0', '2.0'), 0)
  assert.equal(M.naturalVersionCompare('2', '10'), -1, 'numbers compare as numbers, not as text')
  const e = (label, count, rank) => ({ key: label, label, count, rank })
  // Server rank wins when every entry has one.
  assert.deepEqual(M.orderVersionEntries([e('7.2.5', 1, 3), e('7.4.4', 1, 1), e('7.2.8', 1, 2)]).map((x) => x.label), ['7.2.5', '7.2.8', '7.4.4'])
  // Otherwise the natural order of the labels, newest first.
  assert.deepEqual(M.orderVersionEntries([e('7.2.5', 1), e('7.4.4', 1), e('7.2.8', 1)]).map((x) => x.label), ['7.4.4', '7.2.8', '7.2.5'])
  // A partial rank is not trusted.
  assert.deepEqual(M.orderVersionEntries([e('9', 1, 1), e('10', 1)]).map((x) => x.label), ['10', '9'])
}

// Bucket collapsing.
{
  const e = (label, count, extra = {}) => ({ key: label, label, count, href: `/inventory/assets?product=p&product_version=${label}`, ...extra })
  const few = M.collapseVersionBuckets([e('1.0', 5), e('3.0', 10), e('2.0', 1)])
  assert.deepEqual(few.segments.map((s) => s.label), ['3.0', '2.0', '1.0'], 'newest on the left')
  assert.equal(few.total, 16)
  assert.equal(few.distinct, 3)
  assert.deepEqual(few.segments.map((s) => s.kind), ['version', 'version', 'version'])
  assert.equal(few.folded.length, 0)
  assert.equal(few.segments[0].pct, 62.5)
  assert.deepEqual(few.segments.map((s) => s.index), [0, 1, 2])

  // More than the limit: the largest groups keep a segment, the rest fold into one "other", last.
  const many = [e('9', 1), e('8', 2), e('7', 30), e('6', 20), e('5', 10), e('4', 3), e('3', 1), e('2', 40)]
  const c = M.collapseVersionBuckets(many, { maxSegments: 4, otherHref: '/inventory/software/p?tab=versions' })
  assert.deepEqual(c.segments.map((s) => s.label), ['7', '6', '2', '5 other versions'])
  assert.deepEqual(c.segments.map((s) => s.kind), ['version', 'version', 'version', 'other'])
  assert.equal(c.total, 107)
  assert.equal(c.segments.at(-1).count, 17)
  assert.equal(c.segments.at(-1).versions, 5)
  assert.equal(c.segments.at(-1).href, '/inventory/software/p?tab=versions')
  assert.deepEqual(c.folded.map((x) => x.label), ['9', '8', '5', '4', '3'], 'the folded versions are known, newest first')
  assert.equal(c.segments.reduce((n, s) => n + s.count, 0), c.total, 'nothing is lost by collapsing')
  assert.equal(c.distinct, 8)
  // Counts tie: the newer version keeps its segment.
  const tie = M.collapseVersionBuckets([e('1', 5), e('2', 5), e('3', 5)], { maxSegments: 2 })
  assert.deepEqual(tie.segments.map((s) => s.label), ['3', '2 other versions'])

  // Unknown is a value of its own: never folded into other, always last, counted in the total.
  const unk = M.collapseVersionBuckets([e('3', 4), e('2', 3), e('1', 1), { key: '?', label: '', count: 5, unknown: true, href: '/inventory/assets?health=missing_os_version' }], { maxSegments: 2 })
  assert.deepEqual(unk.segments.map((s) => s.kind), ['version', 'other', 'unknown'])
  assert.equal(unk.segments.at(-1).kind, 'unknown')
  assert.equal(unk.segments.at(-1).label, 'Version unknown')
  assert.equal(unk.segments.at(-1).count, 5)
  assert.equal(unk.segments.at(-1).href, '/inventory/assets?health=missing_os_version')
  assert.equal(unk.total, 13)
  assert(!unk.folded.some((x) => x.unknown))
  // Zero-count entries vanish; an empty input is an empty bar.
  assert.equal(M.collapseVersionBuckets([e('1', 0)]).total, 0)
  assert.deepEqual(M.collapseVersionBuckets([]).segments, [])
  // Text for tooltips and screen readers.
  assert.equal(M.segmentText(few.segments[0]), '3.0: 10 hosts (62.5%)')
  assert.equal(M.segmentText({ ...few.segments[2], count: 1, pct: 6.25 }), '1.0: 1 host (6.3%)')
  assert.match(M.barSummary(few), /^3 versions; newest 3\.0 on 10 hosts; 16 hosts in total$/)
  assert.equal(M.barSummary(M.collapseVersionBuckets([])), 'No installations')
}

// Facet items.
{
  const items = [
    { value: 'a', count: 5 }, { value: 'unknown', label: 'Unknown', count: 99, unknown: true }, { value: 'c', count: 5 },
    { value: 'b', count: 50 }, { value: 'd', count: 1 }, { value: 'e', count: 1 }, { value: 'f', count: 1 },
  ]
  assert.deepEqual(M.sortFacetItems(items).map((i) => i.value), ['b', 'a', 'c', 'd', 'e', 'f', 'unknown'], 'largest first, ties by label, unknown last whatever its count')
  const sorted = M.sortFacetItems(items)
  const v = M.visibleFacetItems(sorted, [], { limit: 3 })
  assert.deepEqual(v.shown.map((i) => i.value), ['b', 'a', 'c'])
  assert.equal(v.hidden, 4)
  // A selected value beyond the limit stays visible.
  const pinned = M.visibleFacetItems(sorted, ['f'], { limit: 3 })
  assert.deepEqual(pinned.shown.map((i) => i.value), ['b', 'a', 'c', 'f'])
  assert.equal(pinned.hidden, 3)
  assert.equal(M.visibleFacetItems(sorted, [], { limit: 3, expanded: true }).shown.length, 7)
  assert.deepEqual(M.visibleFacetItems(sorted, [], { limit: 3, filter: 'UNK' }).shown.map((i) => i.value), ['unknown'], 'the text filter searches the whole list, ignoring the limit')
  assert.equal(M.visibleFacetItems(sorted, [], { limit: 10 }).hidden, 0)
}
// The API's facet items: the `unknown` sentinel becomes the unknown row.
assert.deepEqual(M.facetItemsFromApi([{ value: 'windows', label: 'Windows', count: 4 }, { value: 'unknown', label: 'Unknown', count: 2 }]), [
  { value: 'windows', label: 'Windows', count: 4, unknown: undefined },
  { value: 'unknown', label: 'Unknown', count: 2, unknown: true },
])
assert.deepEqual(M.facetItemsFromApi(undefined), [])
// Scope.
assert.deepEqual(M.effectiveScopeTags({ scope_tags: ['dc-east'], role_scope_tags: ['PCI', 'DC-East'] }), ['dc-east', 'PCI'])
assert.deepEqual(M.effectiveScopeTags({}), [])
assert.deepEqual(M.effectiveScopeTags(null), [])

/* ═══ (e) the components, rendered ═══════════════════════════════════════════════════════════ */

const NOW = Date.parse('2026-10-08T12:00:00Z')
const agoIso = (ms) => new Date(NOW - ms).toISOString()

// FreshnessBadge.
{
  const state = (html) => html.match(/data-freshness="(\w+)"/)?.[1]
  const fresh = m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: 'fresh', inventoryAt: agoIso(5 * 60_000), now: NOW }))
  assert.equal(state(fresh), 'fresh')
  assert(fresh.includes('Fresh') && fresh.includes('· 5m ago'), 'label and age in words')
  assert(fresh.includes('success-fg'))
  const aging = m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: 'aging', inventoryAt: agoIso(6 * 3_600_000), lastSeen: agoIso(60_000), now: NOW }))
  assert(aging.includes('Aging') && aging.includes('· 6h ago') && aging.includes('warning-fg'))
  assert(decode(aging).includes('Last seen (reachability): 1m ago'), 'the tooltip separates reachability from data')
  const stale = m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: 'stale', inventoryAt: agoIso(9 * 86_400_000), now: NOW }))
  assert(stale.includes('Stale') && stale.includes('· 9d ago') && stale.includes('danger-fg'))
  const never = m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: 'never', inventoryAt: null, now: NOW }))
  assert(never.includes('Never') && !never.includes('ago') && never.includes('text-text2'))
  // Derived from the timestamp when the server gave no verdict.
  assert.equal(state(m.renderToStaticMarkup(h(m.FreshnessBadge, { kind: 'device', inventoryAt: agoIso(3 * 3_600_000), now: NOW }))), 'aging')
  assert.equal(state(m.renderToStaticMarkup(h(m.FreshnessBadge, { kind: 'server', now: NOW }))), 'never')
  assert(!m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: 'fresh', inventoryAt: agoIso(1000), now: NOW, showAge: false })).includes('ago'))
  // Colour is never the only signal: every state has its word.
  for (const s of ['fresh', 'aging', 'stale', 'never']) assert(m.renderToStaticMarkup(h(m.FreshnessBadge, { freshness: s, now: NOW })).includes(M.FRESHNESS_META[s].label), s)
}

// SourceBadge.
{
  const a = m.renderToStaticMarkup(h(m.SourceBadge, { source: 'entity', at: agoIso(4 * 60_000), showAge: true, now: NOW }))
  assert(a.includes('ENTITY-MIB') && a.includes('· 4m ago') && a.includes('data-source="entity"'))
  assert(decode(m.renderToStaticMarkup(h(m.SourceBadge, { source: 'agent', at: agoIso(4 * 60_000), now: NOW }))).includes('title="Agent, 4m ago"'))
  assert(m.renderToStaticMarkup(h(m.SourceBadge, { source: null })).includes('Unknown source'))
}

// VersionDistributionBar.
{
  const e = (label, count, rank) => ({ key: label, label, count, rank, href: `/inventory/assets?product=google-chrome&product_version=${label}` })
  const entries = [e('150.0.1', 20, 1), e('152.0.8', 41, 3), e('151.0.2', 30, 2), e('149.0.0', 8, 0), e('148.0.0', 3, -1), e('147.0.0', 2, -2), e('146.0.0', 2, -3), e('145.0.0', 1, -4)]
  const html = render(h(m.VersionDistributionBar, { entries, maxSegments: 5, otherHref: '/inventory/software/google-chrome?tab=versions' }))
  const items = [...html.matchAll(/<li[^>]*data-kind="(\w+)"/g)].map((x) => x[1])
  assert.deepEqual(items, ['version', 'version', 'version', 'version', 'other'], 'comparator order, then one other bucket')
  const links = anchors(html)
  assert.equal(links.length, 5, 'every segment is a link')
  assert.deepEqual(links.map((l) => l.attrs['aria-label']), [
    '152.0.8: 41 hosts (38.3%)', '151.0.2: 30 hosts (28.0%)', '150.0.1: 20 hosts (18.7%)', '149.0.0: 8 hosts (7.5%)', '4 other versions: 8 hosts (7.5%)',
  ])
  assert.deepEqual(links.slice(0, 4).map((l) => l.attrs['data-drill-count']), ['41', '30', '20', '8'])
  assert.equal(links[4].attrs['data-drill-count'], undefined, 'the other bucket is not one list, so it makes no drill-count claim')
  assert.equal(links[4].href, '/inventory/software/google-chrome?tab=versions')
  assert.equal(links[0].href, '/inventory/assets?product=google-chrome&product_version=152.0.8')
  for (const l of links) assert.equal(l.attrs.title, l.attrs['aria-label'], 'the tooltip says what a screen reader hears')
  assert.match(decode(html), /<ul[^>]*aria-label="8 versions; newest 152\.0\.8 on 41 hosts; 107 hosts in total"/)
  // It is a real list: a screen reader announces the versions.
  assert((html.match(/<li/g) ?? []).length === 5 && html.includes('role="list"'))
  // With a legend, the legend carries the links and the bar is a visual aid (no duplicate tab stops).
  const legend = render(h(m.VersionDistributionBar, { entries, maxSegments: 5, showLegend: true, otherHref: '/x' }))
  assert.equal(anchors(legend).length, 5)
  assert(legend.includes('152.0.8') && legend.includes('41 · 38.3%'))
  // UI-15: a legend link read on its own names what the versions are of.
  const named = render(h(m.VersionDistributionBar, { entries, maxSegments: 5, showLegend: true, otherHref: '/x', subject: 'FortiOS' }))
  assert(anchors(named).length === 5 && anchors(named).every((a) => a.attrs['aria-label']?.startsWith('FortiOS ')), 'UI-15: the legend links carry the product')
  assert(/subject=\{r\.label\}/.test(read('src/components/inventory/OverviewPanels.tsx')), 'and the Overview passes it')
  // Unknown version: hatched, labelled, counted, linked.
  const withUnknown = render(h(m.VersionDistributionBar, { entries: [e('2', 4, 2), e('1', 3, 1), { key: '?', label: '', count: 5, unknown: true, href: '/inventory/assets?health=missing_os_version' }] }))
  assert(decode(withUnknown).includes('Version unknown: 5 hosts (41.7%)'))
  assert(withUnknown.includes('repeating-linear-gradient'), 'unknown is a pattern, not a colour')
  // Empty.
  assert(render(h(m.VersionDistributionBar, { entries: [] })).includes('No installations'))
  // A segment without an href is not a link (the consumer decides what it drills to).
  assert.equal(anchors(render(h(m.VersionDistributionBar, { entries: [{ key: 'a', label: 'a', count: 1 }] }))).length, 0)
  // Drill equality for the bar: the product filter in each href selects exactly the hosts counted.
  const hosts = [...Array(10)].map((_, i) => ({ id: i, version: i < 6 ? '2' : '1' }))
  const bar = render(h(m.VersionDistributionBar, { entries: [e('2', 6, 2), e('1', 4, 1)] }))
  for (const l of anchors(bar)) {
    const f = F.parseAssetFilters(new URL(l.href, 'http://x').searchParams).filters
    assert.equal(hosts.filter((x) => x.version === f.product_version).length, Number(l.attrs['data-drill-count']))
  }
}

// FacetPanel.
{
  const groups = [
    { key: 'kind', label: 'Kind', items: [{ value: 'server', label: 'Server', count: 128 }, { value: 'device', label: 'Network device', count: 284 }] },
    { key: 'vendor', label: 'Vendor', unknownPatch: F.UNKNOWN_FILTER.vendor, items: [{ value: 'Fortinet', count: 38 }, { value: '', label: 'Unknown', count: 8, unknown: true }] },
    { key: 'tag', label: 'Tags', multi: true, items: [{ value: 'pci', count: 12 }, { value: 'dc-east', count: 40 }] },
    { key: 'freshness', label: 'Freshness', items: [] },
  ]
  const hrefFor = (patch) => `/inventory/assets?patch=${encodeURIComponent(JSON.stringify(patch))}`
  const panel = (filters) => render(h(m.FacetPanel, { groups, filters, hrefFor }))
  const html = panel({})
  const links = anchors(html)
  assert.equal(links.length, 6, 'every value is a link')
  assert(html.includes('<nav') && html.includes('aria-label="Filters"'))
  assert((html.match(/<h2>/g) ?? []).length === 4 && (html.match(/aria-expanded="true"/g) ?? []).length === 4, 'four collapsible groups, open by default')
  assert(html.includes('No values in this selection'), 'an empty group says so (UI-09: not "yet", the data is not missing)')
  const patchOf = (a) => JSON.parse(new URL(a.href, 'http://x').searchParams.get('patch'))
  // Values are listed largest first, the unknown row last in its group.
  assert.deepEqual(patchOf(links[0]), { kind: 'device' })
  assert.deepEqual(patchOf(links[1]), { kind: 'server' })
  assert.deepEqual(patchOf(links[2]), { vendor: 'Fortinet' })
  assert.deepEqual(patchOf(links[3]), { vendor: 'unknown' }, 'the unknown row uses its address, never a free string')
  assert.deepEqual(patchOf(links[4]), { tag: ['dc-east'] })
  assert.equal(links[0].attrs['data-drill-count'], '284')
  assert(html.indexOf('Network device') < html.indexOf('>Server<'), 'largest first')
  // Counts are exact.
  assert(html.includes('>128<') && html.includes('>284<'))
  // Selected: aria-current, a filled box and bold text, and the link removes the filter.
  const on = anchors(panel({ kind: 'device', vendor: 'unknown', tag: ['PCI'] }))
  assert.equal(on[0].attrs['aria-current'], 'true')
  assert.deepEqual(patchOf(on[0]), { kind: null }, 'clicking a selected value clears it')
  assert.equal(on[0].attrs['data-drill-count'], undefined, 'a link that clears a filter makes no drill-count claim')
  assert.equal(on[1].attrs['aria-current'], undefined)
  assert.deepEqual(patchOf(on[1]), { kind: 'server' }, 'single-valued: choosing another value replaces')
  assert.equal(on[2].attrs['aria-current'], undefined, 'Fortinet is not selected')
  assert.equal(on[3].attrs['aria-current'], 'true', 'the unknown row is selected through vendor=unknown')
  assert.deepEqual(patchOf(on[3]), { vendor: null })
  assert.deepEqual(patchOf(on[4]), { tag: ['PCI', 'dc-east'] }, 'tag is repeatable: toggling on adds')
  assert.equal(on[5].attrs['aria-current'], 'true', 'tags match ignoring case')
  assert.deepEqual(patchOf(on[5]), { tag: [] }, 'toggling off the only tag')
  assert(panel({ kind: 'device' }).includes('bg-primary text-on-primary'), 'a selected value has a filled box')
  // Pure helpers.
  assert.equal(FP.selectedCount(groups[0], { kind: 'device' }), 1)
  assert.equal(FP.selectedCount(groups[2], { tag: ['pci', 'dc-east'] }), 2)
  assert.equal(FP.selectedCount(groups[1], { vendor: 'unknown' }), 1)
  assert.equal(FP.selectedCount(groups[1], { vendor: 'Fortinet' }), 1)
  // UI-09: a selected value the API did not list (it matches nothing) is still shown, ticked, with a count of 0.
  {
    const none = [
      { key: 'vendor', label: 'Vendor', unknownPatch: F.UNKNOWN_FILTER.vendor, items: [{ value: 'Juniper Networks', count: 26 }] },
      { key: 'os_family', label: 'OS family', unknownPatch: F.UNKNOWN_FILTER.os_family, items: [] },
      { key: 'tag', label: 'Tags', multi: true, items: [] },
    ]
    const sel = anchors(render(h(m.FacetPanel, { groups: none, filters: { vendor: 'Cisco', os_family: 'junos', tag: ['pci'] }, hrefFor })))
    const cur = sel.filter((a) => a.attrs['aria-current'] === 'true')
    assert.equal(cur.length, 3, 'the vendor, the OS family and the tag that were selected are listed and ticked')
    assert.deepEqual(cur.map(patchOf), [{ vendor: null }, { os_family: null }, { tag: [] }], 'each one removes itself')
    const text = render(h(m.FacetPanel, { groups: none, filters: { vendor: 'Cisco', os_family: 'junos', tag: ['pci'] }, hrefFor }))
    assert(text.includes('Cisco') && text.includes('Junos') && !text.includes('No values in this selection'))
    assert(render(h(m.FacetPanel, { groups: none, filters: { vendor: 'unknown' }, hrefFor })).includes('Unknown'), 'the unknown sentinel is listed as the word Unknown')
    assert.equal(FP.selectedCount(none[0], { vendor: 'Cisco' }), 1)
    assert.equal(FP.withSelectedValues(none[0], { vendor: 'juniper networks' }).length, 1, 'a listed value (any case) is not added twice')
    // FE-14: the open/closed state is stored per user.
    assert.notEqual(FP.facetStorageKey('u1'), FP.facetStorageKey('u2'))
    assert.equal(FP.facetStorageKey(null), 'zp-inventory-facets:anonymous')
  }
  // Long groups can be filtered and cut.
  const long = { key: 'model', label: 'Model', items: Array.from({ length: 20 }, (_, i) => ({ value: `M-${i}`, count: 20 - i })) }
  const longHtml = render(h(m.FacetPanel, { groups: [long], filters: { model: 'M-19' }, hrefFor }))
  assert(longHtml.includes('type="search"') && longHtml.includes('Show 11 more'), 'a long group offers a filter box and Show more (the selected value beyond the limit stays listed)')
  assert(longHtml.includes('M-19'))
  assert(!longHtml.includes('>M-12<'))
}

// InventoryMetaLine.
const SUMMARY = {
  generated_at: '2026-10-08T09:05:12Z',
  scope: { restricted: false, tags: [] },
  assets: { total: 412, server: 128, device: 284, linked_devices: 9 },
  coverage: { fresh: 389, aging: 15, stale: 6, never: 2, not_applicable: 0, fresh_pct: 94.4, fresh_hours: { server: 24, device: 2 }, stale_days: 7 },
  os: { families: 9, releases: 31, unknown: 5, top: [] },
  software: { products: 1840, installs: 39120, publishers: 410, hidden_categories: ['update', 'driver', 'os-component'], top: [], sprawl: [], sprawl_basis: { scope: 'all_products', candidates: 1840, products: 1840 }, security_agent_coverage: { covered: 121, missing: 4, unknown: 3, total: 128 } },
  hardware: { devices: 284, vendors: 12, models: 57, top_models: [] },
  changes_7d: { since: '2026-10-01T09:05:12Z', total: 431, software: { added: 58, removed: 21, changed: 340, upgraded: 300, downgraded: 4 }, os: 4, firmware: 7, hardware: 1 },
  health: { missing_os_version: 9, missing_model: 22, missing_serial: 31, unknown_vendor: 8, stale: 6, never: 2, software_truncated: 1, missing_security_agent: 4, not_collected_by_design: { count: 0 } },
  data_status: { normalising: false, pending_software_rows: 0, pending_identity_rows: 0 },
  lifecycle: null,
}
{
  const html = render(h(m.InventoryMetaLine, { summary: SUMMARY, now: NOW }))
  const text = decode(html.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ')
  assert.match(text, /Inventory as of 09:05 .*94\.4% fresh .*15 aging .*6 stale .*2 never reported/)
  const links = anchors(html)
  assert.deepEqual(links.map((l) => l.href), [
    '/inventory/assets?freshness=fresh', '/inventory/assets?freshness=aging', '/inventory/assets?freshness=stale', '/inventory/assets?freshness=never',
  ], 'Clicking "6 stale" opens Assets filtered freshness=stale')
  assert.deepEqual(links.map((l) => l.attrs['data-drill-count']), ['389', '15', '6', '2'])
  assert(!html.includes('Showing assets tagged'), 'an unrestricted user gets no scope badge')
  // Drill equality: the list behind each link has exactly the number shown.
  const fleet = [...Array(412)].map((_, i) => ({ f: i < 389 ? 'fresh' : i < 404 ? 'aging' : i < 410 ? 'stale' : 'never' }))
  for (const l of links) {
    const f = F.parseAssetFilters(new URL(l.href, 'http://x').searchParams).filters
    assert.equal(fleet.filter((a) => a.f === f.freshness).length, Number(l.attrs['data-drill-count']), l.href)
  }
  // Scoped users see whose assets these numbers cover.
  const scoped = render(h(m.InventoryMetaLine, { summary: SUMMARY, scopeTags: ['dc-east', 'pci', 'lab', 'edge'], now: NOW }))
  assert(scoped.includes('Showing assets tagged dc-east, pci, lab +1'))
  // All clear.
  const clear = render(h(m.InventoryMetaLine, { summary: { ...SUMMARY, coverage: { ...SUMMARY.coverage, fresh: 412, aging: 0, stale: 0, never: 0, fresh_pct: 100 } }, now: NOW }))
  assert(clear.includes('100.0%') && clear.includes('every asset that can report did so within its target'))
  assert.equal(anchors(clear).length, 1)
  // FE-13: 100.0% only when every asset that can report did; 1,999 of 2,000 reads 99.9% next to its "1 aging".
  const almost = decode(render(h(m.InventoryMetaLine, { summary: { ...SUMMARY, assets: { total: 2000, server: 0, device: 2000 }, coverage: { ...SUMMARY.coverage, fresh: 1999, aging: 1, stale: 0, never: 0, fresh_pct: 100 } }, now: NOW })).replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ')
  assert(almost.includes('99.9% fresh') && almost.includes('1 aging') && !almost.includes('100.0%'), almost)
  // X3: assets that cannot report by design are their own count: not "never", not in the share, not in the all-clear.
  const byDesign = render(h(m.InventoryMetaLine, { summary: { ...SUMMARY, assets: { total: 412, server: 128, device: 284 }, coverage: { ...SUMMARY.coverage, fresh: 380, aging: 20, stale: 4, never: 0, not_applicable: 8, fresh_pct: 94.1 } }, now: NOW }))
  const byDesignText = decode(byDesign.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ')
  assert(byDesignText.includes('94.1% fresh') && byDesignText.includes('8 not reported by design'), byDesignText)
  assert(!byDesignText.includes('never reported'), 'by-design assets are not "never"')
  assert(anchors(byDesign).some((l) => l.href === '/inventory/assets?freshness=not_applicable' && l.attrs['data-drill-count'] === '8'), 'the by-design count is a drill-down to freshness=not_applicable')
  // Only by-design assets: no share and no all-clear.
  const onlyDesign = decode(render(h(m.InventoryMetaLine, { summary: { ...SUMMARY, assets: { total: 8, server: 0, device: 8 }, coverage: { ...SUMMARY.coverage, fresh: 0, aging: 0, stale: 0, never: 0, not_applicable: 8, fresh_pct: null }, }, now: NOW })).replace(/<[^>]+>/g, ' '))
  assert(!/fresh/.test(onlyDesign.replace('not reported by design', '')) && !onlyDesign.includes('every asset'), 'nothing can report: no percentage and no all-clear')
  // No assets at all.
  assert(render(h(m.InventoryMetaLine, { summary: { ...SUMMARY, assets: { total: 0, server: 0, device: 0 }, coverage: { fresh: 0, aging: 0, stale: 0, never: 0, fresh_pct: null } }, now: NOW })).includes('no assets yet'))
  // Loading, failed, and failed with the last data.
  assert(render(h(m.InventoryMetaLine, { loading: true })).includes('aria-busy="true"'))
  const failed = render(h(m.InventoryMetaLine, { failed: true, onRetry: () => {} }))
  assert(failed.includes('unavailable') && failed.includes('Retry'))
  assert(render(h(m.InventoryMetaLine, { summary: SUMMARY, failed: true, onRetry: () => {}, now: NOW })).includes('may be out of date'))
}

// Every key of summary.health is a predicate of the asset vocabulary, so every health count is a worklist.
assert.deepEqual(Object.keys(SUMMARY.health).filter((k) => k !== 'not_collected_by_design').sort(), [...F.HEALTH_PREDICATES].sort())

// InventoryEmpty.
{
  assert(render(h(m.InventoryEmpty, { kind: 'no-data' }, 'Install the agent.')).includes('Nothing here yet'))
  assert(render(h(m.InventoryEmpty, { kind: 'no-match' })).includes('Nothing matches these filters'))
  assert(render(h(m.InventoryEmpty, { kind: 'no-scope' })).includes('Nothing in your scope'))
  assert(render(h(m.InventoryEmpty, { kind: 'no-match', title: 'No assets match' })).includes('data-empty="no-match"'))
}

// Knowledge-base links.
{
  assert.equal(KB.KB_BASE, 'https://zentryc.com/kb/zenplus/inventory')
  assert.equal(KB.inventoryKbUrl('overview'), 'https://zentryc.com/kb/zenplus/inventory/')
  assert.equal(KB.kbArticleForPath('/inventory'), 'overview')
  assert.equal(KB.kbArticleForPath('/inventory/assets'), 'assets')
  assert.equal(KB.kbArticleForPath('/inventory/os'), 'operating-systems')
  assert.equal(KB.kbArticleForPath('/inventory/software'), 'software')
  assert.equal(KB.kbArticleForPath('/inventory/software/google-chrome'), 'software', 'the product page belongs to Software')
  assert.equal(KB.kbArticleForPath('/inventory/hardware/'), 'hardware')
  assert.equal(KB.kbArticleForPath('/inventory/changes'), 'changes')
  assert.equal(KB.kbArticleForPath('/inventory/settings'), 'settings')
  for (const slug of Object.keys(KB.KB_ARTICLES)) assert(KB.inventoryKbUrl(slug).startsWith('https://zentryc.com/kb/zenplus/inventory/'))
}

// Access gate (T-UI3).
const USER = (permissions) => ({ id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions })
function setUser(user) {
  // Server rendering reads a zustand store's initial state, so the user goes there as well as into the live state.
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const gateAt = (user, url = '/inventory/assets') => {
  setUser(user)
  return m.renderToStaticMarkup(
    h(m.MemoryRouter, { initialEntries: [url] },
      h(m.Routes, null, h(m.Route, { element: h(m.InventoryAccessGate) }, h(m.Route, { path: 'inventory/*', element: h('main', { id: 'page' }, 'THE PAGE') })))),
  )
}
{
  assert.equal(AX.inventoryAccess(null), 'unknown')
  assert.equal(AX.inventoryAccess({}), 'unknown')
  assert.equal(AX.inventoryAccess({ permissions: [] }), 'denied')
  assert.equal(AX.inventoryAccess({ permissions: ['devices.view', 'servers.view'] }), 'denied')
  assert.equal(AX.inventoryAccess({ permissions: ['inventory.export'] }), 'denied', 'export alone is not view')
  assert.equal(AX.inventoryAccess({ permissions: ['inventory.view'] }), 'allowed')
  assert.equal(AX.inventoryAccess({ permissions: ['system.admin'] }), 'allowed', 'system.admin implies everything, as on the server')
  assert.equal(AX.INVENTORY_VIEW_PERMISSION, 'inventory.view')
  const denied = gateAt(USER(['devices.view']))
  assert(!denied.includes('THE PAGE'), 'no page renders without inventory.view')
  assert(denied.includes("You don&#x27;t have access to Inventory") || denied.includes("You don't have access to Inventory"))
  assert(denied.includes('role="status"') && denied.includes('inventory.view'))
  assert.match(textOf(denied), /Ask an administrator to add inventory\.view to your role/, 'names the permission and what to do')
  assert(denied.includes('href="/"'), 'a way out, not a dead end')
  assert(gateAt(USER(['inventory.view'])).includes('THE PAGE'))
  assert(gateAt(USER(['system.admin'])).includes('THE PAGE'))
  assert(gateAt(USER(undefined)).includes('THE PAGE'), 'permissions not loaded yet: the page renders and the server decides (as RouteGate)')
  assert(gateAt(null).includes('THE PAGE'))
}

// Layout: heading, documentation link, permission-filtered tab strip, meta-line; the product page owns the canvas.
function layoutAt(url, permissions, summary) {
  setUser(USER(permissions))
  const qc = new m.QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (summary) qc.setQueryData(['inventory', 'summary'], summary)
  return m.renderToStaticMarkup(
    h(m.QueryClientProvider, { client: qc },
      h(m.MemoryRouter, { initialEntries: [url] },
        h(m.Routes, null, h(m.Route, { path: 'inventory', element: h(m.InventoryLayout) }, h(m.Route, { path: '*', element: h('div', { id: 'page' }, 'PAGE BODY') }))))),
  )
}
{
  // The layout reads the clock itself (serverNow), so the summary it shows is stamped "now": a fixed
  // date in the fixture would render with its day once the calendar moves on (the label drops the day
  // only for today).
  const generatedAt = new Date().toISOString()
  const view = layoutAt('/inventory/assets', ['inventory.view'], { ...SUMMARY, generated_at: generatedAt })
  assert.match(view, /<h1[^>]*>.*Inventory<a /s, 'heading, with the documentation link beside it')
  assert(decode(view).includes('Every operating system, software title and hardware model across servers and network devices.'))
  const tabs = anchors(view).filter((a) => a.attrs.class?.includes('border-b-2'))
  assert.deepEqual(tabs.map((t) => t.href), ['/inventory', '/inventory/assets', '/inventory/os', '/inventory/software', '/inventory/hardware', '/inventory/changes', '/inventory/settings'], 'UI-07: a view-only role gets every tab, Settings included (read-only), in sidebar order')
  assert(view.includes('aria-label="Inventory sections"'))
  assert.equal(tabs.filter((t) => t.attrs['aria-current'] === 'page').map((t) => t.href).join(), '/inventory/assets', 'the current tab is marked')
  assert(view.includes('https://zentryc.com/kb/zenplus/inventory/assets/'), 'the documentation link follows the tab')
  assert(view.includes('PAGE BODY'))
  assert(textOf(view).includes(`Inventory as of ${M.asOfLabel(generatedAt, Date.now())}`), 'the meta-line is on every page')
  assert(!/Inventory as of \d{4}-/.test(textOf(view)), 'a summary generated today is labelled with the time only')
  const manage = layoutAt('/inventory', ['inventory.view', 'inventory.manage'], SUMMARY)
  assert(anchors(manage).some((a) => a.href === '/inventory/settings'), 'the Settings tab')
  assert.equal(anchors(manage).filter((a) => a.attrs.class?.includes('border-b-2')).length, 7)
  // RG-06: manage WITHOUT view opens nothing (the API refuses the read without view): no tabs at all.
  assert.equal(anchors(layoutAt('/inventory', ['inventory.manage'], SUMMARY)).filter((a) => a.attrs.class?.includes('border-b-2')).length, 0)
  assert(manage.includes('href="https://zentryc.com/kb/zenplus/inventory/"'), 'the Overview article')
  // The Overview tab is exact: only /inventory itself marks it.
  const overviewTabs = anchors(layoutAt('/inventory', ['inventory.view'], SUMMARY)).filter((a) => a.attrs.class?.includes('border-b-2'))
  assert.equal(overviewTabs.filter((t) => t.attrs['aria-current'] === 'page').map((t) => t.href).join(), '/inventory')
  // The meta-line shows its loading state before the summary arrives.
  assert(layoutAt('/inventory', ['inventory.view']).includes('aria-busy="true"'))
  // Product page: back link, no heading, no tab strip, still the meta-line.
  const product = layoutAt('/inventory/software/google-chrome', ['inventory.view'], SUMMARY)
  assert(!product.includes('Inventory sections') && !product.includes('<h1'))
  assert(product.includes('href="/inventory/software"') && textOf(product).includes('Software'))
  assert(product.includes('PAGE BODY') && product.includes('Inventory as of'))
  assert(m.isProductPath('/inventory/software/google-chrome') && m.isProductPath('/inventory/software/7-zip?tab=x'.split('?')[0]))
  assert(!m.isProductPath('/inventory/software') && !m.isProductPath('/inventory/software/') && !m.isProductPath('/inventory/assets'))
  // Tabs from the navigation tree, by permission.
  const can = (perms) => NA.canFromPermissions(perms)
  assert.deepEqual(m.inventoryTabs(can([])).map((t) => t.label), [])
  assert.deepEqual(m.inventoryTabs(can(['inventory.view'])).map((t) => t.label), ['Overview', 'Assets', 'Operating Systems', 'Software', 'Hardware', 'Changes', 'Inventory Settings'])
  assert.deepEqual(m.inventoryTabs(can(['inventory.manage'])).map((t) => t.label), [], 'RG-06: manage without view sees no tab')
  assert.equal(m.inventoryTabs(can(['inventory.view', 'inventory.manage'])).at(-1).label, 'Inventory Settings')
}

// Scoped users: the meta-line badge comes from the account and from the server.
{
  const scopedUser = { ...USER(['inventory.view']), scope_tags: ['dc-east'], role_scope_tags: ['pci'] }
  setUser(scopedUser)
  const qc = new m.QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['inventory', 'summary'], { ...SUMMARY, scope: undefined }) // a summary that does not say: the account's own tags speak
  const html = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: ['/inventory'] }, h(m.Routes, null, h(m.Route, { path: 'inventory', element: h(m.InventoryLayout) }, h(m.Route, { index: true, element: h('div') }))))))
  assert(html.includes('Showing assets tagged dc-east, pci'))
  qc.setQueryData(['inventory', 'summary'], { ...SUMMARY, scope: { restricted: true, tags: ['edge'] } })
  const fromServer = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: ['/inventory'] }, h(m.Routes, null, h(m.Route, { path: 'inventory', element: h(m.InventoryLayout) }, h(m.Route, { index: true, element: h('div') }))))))
  assert(fromServer.includes('Showing assets tagged edge') && !fromServer.includes('dc-east'), "the server's statement of the scope wins")
  qc.setQueryData(['inventory', 'summary'], { ...SUMMARY, scope: { restricted: false, tags: [] } })
  const open = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: ['/inventory'] }, h(m.Routes, null, h(m.Route, { path: 'inventory', element: h(m.InventoryLayout) }, h(m.Route, { index: true, element: h('div') }))))))
  assert(!open.includes('Showing assets tagged'), 'the server says the numbers are not narrowed, so no badge')
}

// The settings page names the missing permission for a role without inventory.manage.
{
  setUser(USER(['inventory.view']))
  const view = render(h(m.InventorySettingsPage))
  assert(view.includes('inventory.manage') && !view.includes('Data &amp; retention'))
  setUser(USER(['inventory.view', 'inventory.manage']))
  assert(render(h(m.InventorySettingsPage)).includes('Data &amp; retention'))
}

/* ═══ (f) URL state ═════════════════════════════════════════════════════════════════════════ */

{
  const cfg = U.urlStateConfig(F.assetFilterCodec, { tabs: ['versions', 'installed', 'evidence'], defaultTab: 'versions' })
  const read = (q) => U.readUrlState(sp(q), cfg)
  const id = '3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
  const s = read(`vendor=Cisco&tag=a&tag=b&sort=ip&order=desc&page=3&page_size=100&tab=installed&asset=device:${id}`)
  assert.deepEqual(s.filters, { vendor: 'Cisco', tag: ['a', 'b'] })
  assert.deepEqual([s.sort, s.order, s.page, s.pageSize, s.tab, s.asset], ['ip', 'desc', 3, 100, 'installed', `device:${id}`])
  const d = read('')
  assert.deepEqual([d.sort, d.order, d.page, d.pageSize, d.tab, d.asset], ['name', 'asc', 1, 50, 'versions', null], 'defaults')
  assert.equal(read('tab=nonsense').tab, 'versions')
  assert.equal(read('asset=device:nope').asset, null)
  assert.equal(read('kind=router').rejected[0].key, 'kind', 'rejected values are reported to the page')
  // Writing: a new filter set goes to page 1 and closes the flyout; other parameters survive.
  const start = sp(`vendor=Cisco&page=4&asset=device:${id}&sort=ip&order=desc&tab=installed`)
  const next = U.withFilters(start, cfg, { vendor: 'Fortinet', kind: 'device' })
  assert.equal(next.toString(), 'kind=device&vendor=Fortinet&sort=ip&order=desc&tab=installed', 'filters first, everything else kept in place')
  assert.equal(next.get('page'), null)
  assert.equal(next.get('asset'), null)
  assert.equal(next.get('sort'), 'ip')
  assert.equal(next.get('tab'), 'installed')
  assert.deepEqual(F.parseAssetFilters(next).filters, { kind: 'device', vendor: 'Fortinet' })
  assert.equal(U.withFilters(start, cfg, {}).get('vendor'), null, 'clearing removes every filter key')
  // Tab: the default tab is not written; switching resets the table's own state.
  assert.equal(U.withTab(start, cfg, 'evidence').get('tab'), 'evidence')
  assert.equal(U.withTab(start, cfg, 'evidence').get('sort'), null)
  assert.equal(U.withTab(start, cfg, 'evidence').get('page'), null)
  assert.equal(U.withTab(start, cfg, 'versions').get('tab'), null)
  assert.equal(U.withTab(start, cfg, 'bogus').get('tab'), null)
  assert.equal(U.withTab(start, cfg, 'evidence').get('vendor'), 'Cisco', 'filters survive a tab change')
  // Sort: new column ascending, same column flips, always back to page 1, defaults are not written.
  const byName = U.withSort(sp('page=3'), cfg, 'ip')
  assert.deepEqual([byName.get('sort'), byName.get('order'), byName.get('page')], ['ip', 'asc', null])
  const flip = U.withSort(byName, cfg, 'ip')
  assert.deepEqual([flip.get('sort'), flip.get('order')], ['ip', 'desc'])
  const toDefault = U.withSort(sp('sort=name&order=desc'), cfg, 'name')
  assert.equal(toDefault.toString(), '', 'name ascending is the default, so the URL is plain again')
  assert.equal(U.withSort(sp(''), cfg, 'colour').toString(), '', 'an unknown column is ignored')
  // Paging.
  assert.equal(U.withPage(sp('vendor=Cisco'), cfg, 3).toString(), 'vendor=Cisco&page=3')
  assert.equal(U.withPage(sp('page=3'), cfg, 1).toString(), '')
  assert.equal(U.withPage(sp(''), cfg, -4).toString(), '')
  assert.equal(U.withPageSize(sp('page=3'), cfg, 100).toString(), 'page_size=100')
  assert.equal(U.withPageSize(sp('page_size=100'), cfg, 50).toString(), '')
  assert.equal(U.withPageSize(sp(''), cfg, 9999).get('page_size'), '500')
  // The open asset.
  assert.equal(U.withAsset(sp('vendor=Cisco'), `device:${id}`).toString(), `vendor=Cisco&asset=device%3A${id}`)
  assert.equal(U.withAsset(sp(`asset=device:${id}`), null).toString(), '')
  assert.equal(U.withAsset(sp(''), 'device:nope').toString(), '')
  // The request: the same parameter names as the URL, sort/order/page/page_size always explicit; page-only keys never leak in.
  const api = U.apiParamsFor(sp(`vendor=Cisco&tag=a&tab=installed&asset=device:${id}&page=2`), cfg)
  assert.equal(api.toString(), 'vendor=Cisco&tag=a&sort=name&order=asc&page=2&page_size=50')
  assert(!api.has('tab') && !api.has('asset'))
  // Push versus replace: typing in the search box replaces; every other change is a view worth going Back to.
  assert.equal(U.pushesHistory({ q: 'for' }), false)
  assert.equal(U.pushesHistory({ vendor: 'Cisco' }), true)
  assert.equal(U.pushesHistory({ q: 'for', kind: 'server' }), true)
  assert.equal(U.pushesHistory({ tag: [] }), true)
}

// The hook, rendered at a URL: it reads the vocabulary from the address and builds links with the same one.
{
  function Probe() {
    const url = m.useInventoryUrlState()
    return h('pre', { id: 'probe' }, JSON.stringify({
      filters: url.filters, rejected: url.rejected, sort: url.sort, order: url.order, page: url.page, pageSize: url.pageSize,
      api: url.apiParams.toString(), count: url.activeFilterCount,
      with: url.hrefWith({ vendor: 'Cisco' }), without: url.hrefWith({ kind: null }),
    }))
  }
  const out = (url) => JSON.parse(decode(render(h(Probe), url).replace(/^<pre[^>]*>/, '').replace(/<\/pre>$/, '')))
  const r = out('/inventory/assets?kind=server&os_family=windows&tag=pci&health=missing_os_version&sort=ip&page=2&kind=device&colour=red')
  assert.deepEqual(r.filters, { kind: 'server', os_family: 'windows', tag: ['pci'], health: 'missing_os_version' })
  assert.deepEqual([r.sort, r.order, r.page, r.pageSize, r.count], ['ip', 'asc', 2, 50, 4])
  assert.equal(r.api, 'kind=server&os_family=windows&tag=pci&health=missing_os_version&sort=ip&order=asc&page=2&page_size=50')
  assert.equal(r.with, '/inventory/assets?kind=server&os_family=windows&vendor=Cisco&tag=pci&health=missing_os_version&sort=ip&colour=red', 'a facet link keeps the other filters and the sort, and drops the page')
  assert.equal(r.without, '/inventory/assets?os_family=windows&tag=pci&health=missing_os_version&sort=ip&colour=red')
  assert.equal(out('/inventory/assets').with, '/inventory/assets?vendor=Cisco')
  assert.equal(out('/inventory/assets').without, '/inventory/assets')
  assert.deepEqual(out('/inventory/assets?vendor=unknown').rejected, [], 'vendor=unknown is the sentinel for a device with no vendor')
  assert.deepEqual(out(`/inventory/assets?vendor=${'x'.repeat(201)}`).rejected.map((x) => x.key), ['vendor'])
}

/* ═══ (g) the golden API fixtures and pages/inventory/types.ts describe the same responses ═════ */

// The backend writes golden responses to src/pages/inventory/fixtures/ (server/tests/test_inventory_fixtures.py);
// every later page is built against them. types.ts is the frontend's single description of those
// responses, so the two are compared structurally here: every field types.ts declares must be in the
// fixture (null is a value, an omitted key is a contract change), and no fixture field may be missing
// from types.ts. The check reads types.ts with the TypeScript compiler, so a field added on either
// side without the other fails this script instead of surfacing as an `undefined` in a page.
{
  const fixturesDir = path.join(src, 'pages/inventory/fixtures')
  assert(fs.existsSync(fixturesDir), 'src/pages/inventory/fixtures/ (the golden API responses) exists')
  const typesSource = ts.createSourceFile('types.ts', read('src/pages/inventory/types.ts'), ts.ScriptTarget.Latest, true)
  const declarations = new Map()
  typesSource.forEachChild((n) => { if (ts.isInterfaceDeclaration(n) || ts.isTypeAliasDeclaration(n)) declarations.set(n.name.text, n) })
  // Types that come from filters.ts (the one vocabulary) and what they allow.
  const enums = {
    Freshness: M.FRESHNESS_STATES, AssetKind: F.ASSET_KINDS, SortOrder: F.SORT_ORDERS, HealthPredicate: F.HEALTH_PREDICATES,
  }
  const K = ts.SyntaxKind
  const isNull = (n) => ts.isLiteralTypeNode(n) && n.literal.kind === K.NullKeyword
  const literals = (n) => (ts.isLiteralTypeNode(n) && ts.isStringLiteral(n.literal) ? [n.literal.text] : null)
  const kindOf = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v)

  /** The closed set of keys a `Record<K, V>` is declared over, or null for an open set (`string`). */
  function keySet(node) {
    if (node.kind === K.StringKeyword) return null
    const direct = literals(node)
    if (direct) return direct
    if (ts.isUnionTypeNode(node)) {
      const all = node.types.map(literals)
      return all.every(Boolean) ? all.flat() : null
    }
    if (ts.isTypeReferenceNode(node)) {
      const name = node.typeName.getText()
      if (enums[name]) return [...enums[name]]
      const d = declarations.get(name)
      if (d && ts.isTypeAliasDeclaration(d)) return keySet(d.type)
    }
    return null
  }

  function conforms(value, node, where, problems) {
    if (ts.isParenthesizedTypeNode(node)) return conforms(value, node.type, where, problems)
    const expect = (type) => { if (kindOf(value) !== type) problems.push(`${where}: expected ${type}, the fixture has ${kindOf(value)}`) }
    if (node.kind === K.StringKeyword) return expect('string')
    if (node.kind === K.NumberKeyword) return expect('number')
    if (node.kind === K.BooleanKeyword) return expect('boolean')
    if (node.kind === K.UnknownKeyword || node.kind === K.AnyKeyword) return undefined
    if (isNull(node)) return value === null ? undefined : problems.push(`${where}: expected null, the fixture has ${kindOf(value)}`)
    const lit = literals(node)
    if (lit) return value === lit[0] ? undefined : problems.push(`${where}: expected "${lit[0]}", the fixture has ${JSON.stringify(value)}`)
    if (ts.isUnionTypeNode(node)) {
      if (value === null) return node.types.some(isNull) ? undefined : problems.push(`${where}: null is not allowed by types.ts`)
      const rest = node.types.filter((t) => !isNull(t))
      const trials = rest.map((t) => { const p = []; conforms(value, t, where, p); return p })
      return trials.some((p) => p.length === 0) ? undefined : problems.push(...(trials[0] ?? [`${where}: no type matches`]))
    }
    if (ts.isArrayTypeNode(node)) {
      if (kindOf(value) !== 'array') return problems.push(`${where}: expected an array, the fixture has ${kindOf(value)}`)
      return value.slice(0, 500).forEach((el, i) => conforms(el, node.elementType, `${where}[${i}]`, problems))
    }
    if (ts.isTypeLiteralNode(node)) return members(value, node.members, where, problems)
    if (ts.isTypeReferenceNode(node)) {
      const name = node.typeName.getText()
      if (name === 'Record') {
        if (kindOf(value) !== 'object') return problems.push(`${where}: expected an object, the fixture has ${kindOf(value)}`)
        const required = keySet(node.typeArguments[0])
        if (required) {
          for (const k of required) if (!(k in value)) problems.push(`${where}.${k}: missing in the fixture`)
          for (const k of Object.keys(value)) if (!required.includes(k)) problems.push(`${where}.${k}: in the fixture but not in types.ts`)
        }
        return Object.entries(value).forEach(([k, v]) => conforms(v, node.typeArguments[1], `${where}.${k}`, problems))
      }
      if (enums[name]) return enums[name].includes(value) ? undefined : problems.push(`${where}: ${JSON.stringify(value)} is not a ${name} (${enums[name].join(', ')})`)
      const decl = declarations.get(name)
      if (!decl) return problems.push(`${where}: types.ts refers to ${name}, which this check cannot resolve`)
      return ts.isInterfaceDeclaration(decl) ? members(value, decl.members, where, problems) : conforms(value, decl.type, where, problems)
    }
    return problems.push(`${where}: type syntax ${ts.SyntaxKind[node.kind]} is not understood by this check`)
  }
  function members(value, list, where, problems) {
    if (kindOf(value) !== 'object') return problems.push(`${where}: expected an object, the fixture has ${kindOf(value)}`)
    const declared = new Set()
    for (const m of list) {
      if (!ts.isPropertySignature(m)) continue
      const key = m.name.getText().replace(/^['"]|['"]$/g, '')
      declared.add(key)
      if (!(key in value)) { if (!m.questionToken) problems.push(`${where}.${key}: missing in the fixture`); continue }
      conforms(value[key], m.type, `${where}.${key}`, problems)
    }
    for (const k of Object.keys(value)) if (!declared.has(k)) problems.push(`${where}.${k}: in the fixture but not in types.ts`)
  }
  const check = (value, typeName, label) => {
    const problems = []
    conforms(value, { kind: K.TypeReference, typeName: { getText: () => typeName }, typeArguments: undefined }, label, problems)
    return problems
  }
  const golden = (file) => JSON.parse(fs.readFileSync(path.join(fixturesDir, file), 'utf8'))

  const pairs = [
    ['summary.json', 'InventorySummary'], ['summary-scoped.json', 'InventorySummary'],
    ['assets.json', 'AssetList'], ['assets-scoped.json', 'AssetList'], ['facets.json', 'FacetsResponse'],
    // Added with the Overview, Assets and flyout pages (the OS tree, hardware and changes are reused by their pages).
    ['asset-server.json', 'AssetDetail'], ['asset-device.json', 'AssetDetail'], ['asset-device-ha.json', 'AssetDetail'],
    ['os.json', 'OsResponse'], ['hardware.json', 'HardwareResponse'], ['changes.json', 'ChangesResponse'], ['changes-scoped.json', 'ChangesResponse'],
    ['trends.json', 'TrendsResponse'], ['trends-scoped.json', 'TrendsResponse'],
  ]
  for (const [file, type] of pairs) assert.deepEqual(check(golden(file), type, file), [], `${file} does not match ${type} in types.ts`)

  // Not vacuous: a dropped field, a new field and a wrong type are each reported.
  const s = golden('summary.json')
  const mutate = (fn) => { const c = structuredClone(s); fn(c); return check(c, 'InventorySummary', 'summary.json') }
  assert(mutate((c) => { delete c.coverage.stale_days }).some((p) => /coverage\.stale_days: missing/.test(p)))
  assert(mutate((c) => { c.coverage.invented = 1 }).some((p) => /coverage\.invented: in the fixture but not in types\.ts/.test(p)))
  assert(mutate((c) => { c.assets.total = '412' }).some((p) => /assets\.total: expected number/.test(p)))
  assert(mutate((c) => { delete c.health.missing_serial }).some((p) => /health\.missing_serial: missing/.test(p)), 'every health predicate is a key')
  assert(mutate((c) => { c.software.top[0].hosts = null }).some((p) => /software\.top\[0\]\.hosts/.test(p)))
  const a = golden('assets.json')
  const badRow = structuredClone(a); badRow.items[3].freshness = 'recent'
  assert(check(badRow, 'AssetList', 'assets.json').some((p) => /items\[3\]\.freshness: "recent" is not a Freshness/.test(p)))

  // The numbers in one golden response agree with each other (the drill-down contract at the data level).
  for (const file of ['summary.json', 'summary-scoped.json']) {
    const x = golden(file)
    assert.equal(x.assets.server + x.assets.device, x.assets.total, `${file}: servers + devices = assets`)
    const c = x.coverage
    assert.equal(c.fresh + c.aging + c.stale + c.never + c.not_applicable, x.assets.total, `${file}: freshness states (not_applicable included) cover every asset`)
    assert.equal(x.health.not_collected_by_design.count, c.not_applicable, `${file}: the by-design health line is the not_applicable bucket`)
    assert.equal(c.fresh_pct, c.fresh + c.aging + c.stale + c.never === 0 ? null : Math.round((1000 * c.fresh) / (x.assets.total - c.not_applicable)) / 10, `${file}: fresh_pct is over the assets that can report`)
    assert.equal(x.software.security_agent_coverage.covered + x.software.security_agent_coverage.missing + x.software.security_agent_coverage.unknown, x.assets.server, `${file}: endpoint protection covered + missing + unknown = every server`)
    assert.equal(x.health.missing_security_agent, x.software.security_agent_coverage.missing, `${file}: the health line is the missing figure`)
    assert.equal(x.health.stale, c.stale, `${file}: health.stale is coverage.stale`)
    assert.equal(x.health.never, c.never, `${file}: health.never is coverage.never`)
    assert.deepEqual(Object.keys(x.health).filter((k) => k !== 'not_collected_by_design').sort(), [...F.HEALTH_PREDICATES].sort(), `${file}: every health predicate has a count`)
  }
  const facets = golden('facets.json')
  const summary = golden('summary.json')
  assert.equal(facets.total, summary.assets.total, 'the unfiltered facets cover the same assets as the summary')
  for (const state of M.FRESHNESS_STATES) {
    const item = facets.facets.freshness.items.find((i) => i.value === state)
    assert.equal(item?.count ?? 0, summary.coverage[state], `facet freshness=${state} equals coverage.${state}`)
  }
  assert.equal(golden('assets.json').total, summary.assets.total, 'the unfiltered asset list totals the summary')
  assert(golden('summary-scoped.json').scope.restricted === true && golden('summary-scoped.json').scope.tags.length > 0, 'the scoped golden summary says whose assets it covers')
  assert(golden('summary-scoped.json').assets.total < summary.assets.total, 'a scoped caller counts fewer assets than the whole fleet')
}

/* ═══ (h) Overview, Assets and the flyout (tasks 1.4 and 1.5) ═════════════════════════════════ */

// The page logic is pure functions (components/inventory/overviewModel.ts, assetModel.ts, changeModel.ts), run here against
// the backend's golden responses; the pages are then rendered from those same responses.
{
  const gdir = path.join(src, 'pages/inventory/fixtures')
  const G = (file) => JSON.parse(fs.readFileSync(path.join(gdir, file), 'utf8'))
  const summary = G('summary.json')
  const scopedSummary = G('summary-scoped.json')
  const osTree = G('os.json')
  const hardware = G('hardware.json')
  const changes = G('changes.json')
  const facets = G('facets.json')
  const assetList = G('assets.json')
  const vocabulary = new Set(F.ASSET_FILTER_KEYS)
  const noNested = (html) => {
    let depth = 0
    for (const tag of html.matchAll(/<(\/?)a(?=[\s>])/g)) {
      depth += tag[1] ? -1 : 1
      assert(depth <= 1, 'a link is nested inside a link')
    }
  }
  /** Every filter set a drill carries is the asset vocabulary and nothing else, and survives the URL round trip. */
  const checkDrill = (d, where) => {
    if (d.type !== 'assets') {
      assert(d.href.startsWith('/inventory/'), `${where}: ${d.href}`)
      return
    }
    for (const k of Object.keys(d.filters)) assert(vocabulary.has(k), `${where}: "${k}" is not in the filter vocabulary`)
    const back = F.parseAssetFilters(new URL(d.href, 'http://x').searchParams)
    assert.deepEqual(back.rejected, [], `${where}: ${d.href} is rejected by the vocabulary`)
    assert.deepEqual(back.filters, d.filters, `${where}: the URL carries exactly the filters`)
    assert(d.href.startsWith('/inventory/assets'), where)
  }
  const drillAt = (d) => (d.type === 'assets' ? new URL(d.href, 'http://x').searchParams.toString() : d.href)

  /* ── the KPI row: the drill-down map of 05-P1-API-CONTRACT.md section 4 ─────────────────── */
  {
    const k = OV.kpiModels(summary)
    assert.deepEqual(k.map((x) => x.id), ['assets', 'os', 'software', 'hardware', 'fresh', 'changes'], 'six cards, in the order of doc 03 section 3.1')
    const by = Object.fromEntries(k.map((x) => [x.id, x]))
    // Assets: the total, servers, network devices.
    assert.equal(by.assets.valueCount, summary.assets.total)
    assert.equal(drillAt(by.assets.drill), '')
    assert.equal(by.assets.drill.count, 145)
    assert.deepEqual(by.assets.parts.map((p) => [p.text, p.value, drillAt(p.drill)]), [['servers', 60, 'kind=server'], ['network', 85, 'kind=device']])
    // OS releases: a page, with the unknown count as its own list.
    assert.equal(by.os.drill.href, '/inventory/os')
    assert.deepEqual(by.os.parts.map((p) => [p.text, p.value, drillAt(p.drill)]), [['families', summary.os.families, '/inventory/os'], ['unknown OS', summary.os.unknown, 'os_family=unknown']])
    // Software: pages.
    assert.equal(by.software.drill.href, '/inventory/software')
    assert.deepEqual(by.software.parts.map((p) => [p.value, p.drill.href]), [[summary.software.installs, '/inventory/software'], [summary.software.publishers, '/inventory/software']])
    // Hardware: models and vendors are the hardware page; the device count is a list.
    assert.equal(by.hardware.drill.href, '/inventory/hardware')
    assert.deepEqual(by.hardware.parts.map((p) => [p.text, p.value, drillAt(p.drill)]), [['vendors', summary.hardware.vendors, '/inventory/hardware'], ['network devices', summary.hardware.devices, 'kind=device']])
    // Fresh: the percentage is derived, and opens the fresh list whose total is the count.
    assert.equal(by.fresh.value, M.fmtShare(summary.coverage.fresh, summary.assets.total - summary.coverage.not_applicable), 'the share is over the assets that can report')
    assert.equal(by.fresh.note, `of ${summary.assets.total - summary.coverage.not_applicable} assets that can report`)
    assert.equal(by.fresh.valueCount, null)
    assert.equal(drillAt(by.fresh.drill), 'freshness=fresh')
    assert.equal(by.fresh.drill.count, summary.coverage.fresh)
    assert.deepEqual(by.fresh.parts.map((p) => [p.value, drillAt(p.drill)]), [[summary.coverage.fresh, 'freshness=fresh'], [summary.coverage.stale + summary.coverage.never, 'freshness=stale_or_never'], [summary.coverage.not_applicable, 'freshness=not_applicable']])
    assert.equal(by.fresh.parts[1].value, summary.coverage.stale + summary.coverage.never, 'stale or never is the union of the two buckets')
    // Changes: since = the summary window; software is added + removed + changed.
    assert.equal(by.changes.drill.href, `/inventory/changes?since=${encodeURIComponent(summary.changes_7d.since)}`)
    assert.equal(by.changes.valueCount, summary.changes_7d.total)
    assert.deepEqual(by.changes.parts.map((p) => [p.text, p.value]), [['software', summary.changes_7d.software.added + summary.changes_7d.software.removed + summary.changes_7d.software.changed], ['firmware', summary.changes_7d.firmware], ['OS', summary.changes_7d.os], ['hardware', summary.changes_7d.hardware]])
    assert(by.changes.parts.every((p) => p.drill.href.includes('category=') && p.drill.href.includes('since=')))
    assert.equal(by.changes.parts.reduce((n, p) => n + p.value, 0), summary.changes_7d.total, 'the parts add up to the total')
    for (const d of OV.kpiDrills(k)) checkDrill(d, 'kpi')
    // Zero parts are left out, never shown as "0 OS".
    const quiet = OV.kpiModels({ ...summary, changes_7d: { ...summary.changes_7d, os: 0, hardware: 0 } }).find((x) => x.id === 'changes')
    assert.deepEqual(quiet.parts.map((p) => p.text), ['software', 'firmware'])
    assert.equal(OV.kpiModels({ ...summary, os: { ...summary.os, unknown: 0 } })[1].parts.length, 1, 'no unknown OS, no unknown link')
    // When no asset can report (every one is "by design") the share is a dash, never 0%; and the note says why.
    const allByDesign = OV.kpiModels({ ...summary, coverage: { ...summary.coverage, fresh: 0, aging: 0, stale: 0, never: 0, not_applicable: summary.assets.total, fresh_pct: null } }).find((x) => x.id === 'fresh')
    assert.equal(allByDesign.value, '—')
    assert.equal(allByDesign.note, 'no asset can report an inventory')
    // FE-13: never 100.0% unless every one that can report did.
    assert.equal(M.fmtShare(1999, 2000), '99.9%')
    assert.equal(M.fmtShare(2000, 2000), '100.0%')
    assert.equal(M.fmtShare(1, 5000), '<0.1%')
    assert.equal(M.fmtShare(0, 5000), '0.0%')
    assert.equal(M.fmtShare(0, 0), '—')
  }

  /* ── servers: donut by family, legend by release ─────────────────────────────────────────── */
  {
    // The golden tree holds every family, so it also exercises the folding of families beyond the fourth.
    const m1 = OV.serverOsModel(osTree)
    assert.equal(m1.total, 145)
    // Slices: the biggest families keep a colour, the rest fold, unknown is its own hatched slice and still a link.
    const keys = m1.slices.map((s) => s.key)
    assert.equal(keys[keys.length - 1], 'unknown')
    assert(m1.slices.length <= OV.MAX_FAMILY_SLICES + 2, 'at most four families, one folded slice and unknown')
    const folded = m1.slices.find((s) => s.other)
    assert(folded && folded.color === undefined && folded.drill.type === 'page', 'folded families have no single filter: they open the OS page')
    const unknown = m1.slices.find((s) => s.unknown)
    assert.deepEqual([unknown.count, unknown.drill.filters], [17, { kind: 'server', os_family: 'unknown' }])
    assert.equal(m1.slices.reduce((n, s) => n + s.count, 0), m1.total, 'the slices add up to every asset: nothing is dropped')
    // Legend: biggest release first, "Windows Server 2022" naming, product and release always in the filter.
    assert.deepEqual(m1.releases.slice(0, 3).map((r) => [r.label, r.count]), [['Windows Server 2022', 18], ['Windows Server 2019', 14], ['Windows Server 2016', 9]])
    assert.deepEqual(m1.releases[0].drill.filters, { kind: 'server', os_family: 'windows', os_product: 'Windows Server', os_release: '2022' })
    assert.equal(m1.releases.length, OV.MAX_RELEASE_ROWS + 1, 'eight releases and the unknown row')
    assert.equal(m1.releases[m1.releases.length - 1].unknown, true)
    assert(m1.more && m1.more.releases > 0 && m1.more.href === '/inventory/os?kind=server')
    const shownCount = m1.releases.reduce((n, r) => n + r.count, 0) + m1.more.assets
    assert.equal(shownCount, m1.total, 'legend rows plus "more" account for every asset')
    for (const r of m1.releases) checkDrill(r.drill, 'release row')
    for (const s of m1.slices) checkDrill(s.drill, 'slice')
    // A null product or release is the sentinel, never a blank.
    const odd = { ...osTree, total: 3, families: [{ family: 'other', label: 'Other', count: 3, products: [{ product: null, count: 3, releases: [{ release: null, count: 3, latest_version: null, distinct_versions: 0, currency: null, versions: [{ version: null, count: 3, raw_example: null }] }] }] }] }
    const o = OV.serverOsModel(odd)
    assert.deepEqual([o.releases[0].label, o.releases[0].drill.filters.os_product, o.releases[0].drill.filters.os_release], ['Other (release unknown)', 'unknown', 'unknown'])
    checkDrill(o.releases[0].drill, 'null levels')
    // No servers.
    assert.equal(OV.serverOsModel({ ...osTree, total: 0, families: [] }).slices.length, 0)
    // Geometry: slices cover the ring, never overlap, a lone slice is the full circle.
    const a = m.sliceAngles([50, 30, 20])
    assert(a[0].start > 0 && a[2].end < 360 && a[0].end < a[1].start && a[1].end < a[2].start)
    assert.equal(Math.round((a[2].end + 0.8) * 10) / 10, 360)
    assert.deepEqual(m.sliceAngles([0, 0]), [{ start: 0, end: 0 }, { start: 0, end: 0 }])
    assert.deepEqual(m.sliceAngles([7]), [{ start: 0, end: 360 }], 'one slice has no gap')
  }

  /* ── network devices: one stacked bar per OS product ─────────────────────────────────────── */
  {
    // The golden tree holds both kinds; the network panel is fed the tree of kind=device (the families of servers removed).
    const devFamilies = osTree.families.filter((f) => !['windows', 'linux'].includes(f.family))
    const devTree = { ...osTree, kind: 'device', families: devFamilies, total: devFamilies.reduce((x, f) => x + f.count, 0) }
    const n = OV.networkOsModel(devTree)
    const forti = n.rows.find((r) => r.label === 'FortiOS')
    assert.deepEqual([forti.count, forti.versions], [14, 4], 'FortiOS: 14 devices on 4 versions (7.4.4, 7.2.8, 7.2.5, 6.4.x, plus an unknown)')
    assert.equal(forti.entries.reduce((x, e) => x + e.count, 0), forti.count, 'the segments add up to the product')
    // Newest first, as the API orders them; the rank keeps that order.
    const known = forti.entries.filter((e) => !e.unknown)
    assert.deepEqual(known.map((e) => e.label), [...known.map((e) => e.label)])
    assert(known.every((e, i) => i === 0 || e.rank < known[i - 1].rank), 'ranks fall from newest to oldest')
    // Every segment is a list of exactly its devices: kind, family, product, release and version.
    for (const e of forti.entries) {
      const f = F.parseAssetFilters(new URL(e.href, 'http://x').searchParams).filters
      assert.equal(f.kind, 'device'); assert.equal(f.os_family, 'fortios'); assert.equal(f.os_product, 'FortiOS')
      if (e.unknown) assert.deepEqual([f.os_version, f.os_release], ['unknown', undefined], 'unknown versions of every release share one list')
      else assert(f.os_release && f.os_version === e.label, e.href)
    }
    const unknownEntries = forti.entries.filter((e) => e.unknown)
    assert.equal(new Set(unknownEntries.map((e) => e.href)).size, 1, 'merged unknown segments link to the one list that holds them all')
    assert.deepEqual(forti.drill.filters, { kind: 'device', os_family: 'fortios', os_product: 'FortiOS' })
    // Products first by count; the unknown OS is last and still a row.
    assert.deepEqual(n.rows.slice(0, 3).map((r) => r.label), ['IOS XE', 'FortiOS', 'AOS-CX'])
    assert(n.rows.length <= OV.MAX_FIRMWARE_PRODUCTS + 1)
    assert.equal(n.rows[n.rows.length - 1].unknown, true)
    assert.deepEqual(n.rows[n.rows.length - 1].drill.filters, { kind: 'device', os_family: 'unknown' })
    assert(n.more && n.more.href === '/inventory/os?kind=device')
    assert.equal(n.rows.reduce((x, r) => x + r.count, 0) + n.more.assets, n.total, 'rows plus "more" are every device')
    for (const r of n.rows) { checkDrill(r.drill, r.label); for (const e of r.entries) checkDrill({ type: 'assets', href: e.href, filters: F.parseAssetFilters(new URL(e.href, 'http://x').searchParams).filters }, e.href) }
  }

  /* ── software, hardware, agent coverage, health ──────────────────────────────────────────── */
  {
    const top = OV.softwareRows(summary.software.top, 'hosts')
    assert.equal(top.length, 8)
    const chrome = top.find((r) => r.key === 'google-chrome')
    const chromeSource = summary.software.top.find((p) => p.product_key === 'google-chrome')
    assert.deepEqual(chrome.hostsDrill.filters, { product: 'google-chrome' })
    assert.equal(chrome.hostsDrill.count, chromeSource.hosts)
    assert.equal(chrome.versionsDrill.href, '/inventory/software/google-chrome')
    assert.equal(chrome.versionsDrill.count, chromeSource.versions, 'the product page has as many version rows as the number it was linked from')
    assert.equal(Math.max(...top.map((r) => r.barPct)), 100)
    assert.equal(OV.productHref('a b/c'), '/inventory/software/a%20b%2Fc', 'a product key is encoded into the path')
    for (const r of [...top, ...OV.softwareRows(summary.software.sprawl, 'versions')]) checkDrill(r.hostsDrill, r.key)
    // Endpoint protection: covered -> present, without -> missing, unknown -> unknown; "of N" is the servers with a complete list (text).
    const agent = OV.agentCoverage(summary)
    const sac = summary.software.security_agent_coverage
    assert.deepEqual([agent.covered, agent.missing, agent.unknown, agent.answerable, agent.total], [sac.covered, sac.missing, sac.unknown, sac.covered + sac.missing, sac.total])
    assert.equal(agent.missing, summary.health.missing_security_agent, 'the gap is the health count')
    assert.equal(agent.covered + agent.missing + agent.unknown, summary.assets.server, 'the three figures are every server')
    assert.deepEqual([agent.coveredDrill.filters, agent.gapDrill.filters, agent.unknownDrill.filters], [{ security_agent: 'present' }, { security_agent: 'missing' }, { security_agent: 'unknown' }])
    assert.deepEqual([agent.coveredDrill.count, agent.gapDrill.count, agent.unknownDrill.count], [sac.covered, sac.missing, sac.unknown])
    assert.equal(OV.agentCoverage({ ...summary, software: { ...summary.software, security_agent_coverage: { covered: 0, missing: 0, unknown: 0, total: 0 } } }), null, 'no server: no line')
    assert.equal(OV.unknownServersText(10), '10 servers unknown: no complete software or service list to judge')
    assert.equal(OV.unknownServersText(1), '1 server unknown: no complete software or service list to judge')
    // Hardware.
    const models = OV.topModels(summary)
    assert.deepEqual(models[0].drill.filters, { kind: 'device', vendor: 'Cisco', model: 'C9200L-24P-4G' })
    assert.equal(models[0].drill.count, 6)
    const mem = OV.memoryRows(hardware)
    assert.deepEqual(mem.map((r) => r.value), hardware.servers.memory.filter((b) => b.value !== 'unknown' || b.count > 0).map((b) => b.value), 'the six buckets, then Unknown when any server has no memory figure')
    assert.equal(mem.reduce((n, r) => n + r.count, 0), hardware.servers.total, 'the buckets, unknown included, are every server')
    assert.deepEqual(mem[0].drill.filters, { memory: 'le8' })
    assert(OV.memoryRows({ ...hardware, servers: { ...hardware.servers, memory: [...hardware.servers.memory, { value: 'unknown', label: 'Unknown', count: 4 }] } }).some((r) => r.unknown && r.drill.filters.memory === 'unknown'), 'unknown memory is a row, counted and a link')
    assert(!OV.memoryRows({ ...hardware, servers: { ...hardware.servers, memory: [{ value: 'unknown', label: 'Unknown', count: 0 }] } }).length, 'an empty unknown bucket is not a row')
    // Health: the eight predicates, in order, each a worklist; then the by-design line (information, never a gap).
    const health = OV.healthItems(summary)
    assert.deepEqual(health.map((i) => i.key), [...F.HEALTH_PREDICATES, 'not_collected_by_design'])
    for (const i of health) {
      if (i.key === 'not_collected_by_design') {
        assert.equal(i.count, summary.health.not_collected_by_design.count)
        assert.deepEqual(i.drill.filters, { freshness: 'not_applicable' }, 'UP-04: the by-design line drills to freshness=not_applicable')
        assert.equal(i.info, true)
        checkDrill(i.drill, i.key)
        continue
      }
      assert.equal(i.count, summary.health[i.key])
      assert.deepEqual(i.drill.filters, { health: i.key })
      assert.equal(i.drill.count, i.count)
      checkDrill(i.drill, i.key)
    }
    // Scope decision S3: the hardware predicates speak of network devices, the agent predicates of servers.
    const text = Object.fromEntries(health.map((i) => [i.key, i.text]))
    assert(text.missing_model.includes('network device') && text.missing_serial.includes('network device') && text.unknown_vendor.includes('network device'))
    assert(text.missing_security_agent.includes('server') && text.software_truncated.includes('server'))
    assert(text.missing_os_version.includes('asset') && text.stale.includes('asset') && text.never.includes('asset'))
    assert.equal(OV.healthItems({ ...summary, health: { ...summary.health, missing_model: 1 } }).find((i) => i.key === 'missing_model').text, 'network device missing a model')
    assert.equal(OV.healthAllClear(summary), false)
    const zero = { ...summary, health: { ...Object.fromEntries(F.HEALTH_PREDICATES.map((k) => [k, 0])), not_collected_by_design: { count: 8 } } }
    assert.equal(OV.healthAllClear(zero), true, 'assets that are not collected by design do not prevent the all-clear')
    assert.equal(OV.healthAllClear({ ...zero, assets: { ...zero.assets, total: 0 } }), false, 'no assets is not "complete"')
    assert.equal(OV.healthAllClear({ ...zero, health: { ...zero.health, stale: 1 } }), false)
    assert.equal(OV.healthAllClear({ ...zero, data_status: { normalising: true, pending_software_rows: 10, pending_identity_rows: 0 } }), false, 'UP-02: while existing lists are normalised nothing is called complete')
    // Wording (UI-01, UP-01, DT-03): endpoint protection, never EDR/AV or "security agent"; the Never line names its cause.
    assert.equal(text.missing_security_agent, health.find((i) => i.key === 'missing_security_agent').count === 1 ? 'server without endpoint protection' : 'servers without endpoint protection')
    assert(/can report but never/.test(text.never), text.never)
    assert(health.find((i) => i.key === 'never').hint.includes('cannot report by design'))
    assert.equal(text.not_collected_by_design.includes('not reported by design'), true)
    assert(!health.some((i) => /EDR|AV\b|security agent/i.test(`${i.text} ${i.none} ${i.hint ?? ''}`)), 'no EDR/AV or security-agent wording in the health lines')
    assert.equal(F.HEALTH_LABELS.missing_security_agent, 'No endpoint protection')
    assert.equal(F.FILTER_LABELS.security_agent, 'Endpoint protection')
    // Version sprawl: the note says what the ranking is and, when it is an approximation, which.
    assert(OV.sprawlNote(summary.software.sprawl_basis).startsWith('Ranked by how many different versions are installed, a sign of upgrade drift.'))
    assert(OV.sprawlNote({ scope: 'top_installed', candidates: 400, products: 5200 }).includes('Only the 400 most installed of 5,200 products were ranked.'))
    assert(!OV.sprawlNote(summary.software.sprawl_basis).includes('Only the'))
  }

  /* ── sparklines: scope decision S4 ───────────────────────────────────────────────────────── */
  {
    const series = (dimension, values) => ({ metric: 'x', dimension, points: values.map((v, i) => ({ day: `2026-10-0${i + 1}`, value: v })) })
    assert.equal(OV.MIN_TREND_DAYS, 3)
    const raw = { restricted: false, series: [series('server', [10, 11, 12]), series('device', [20, 21, 22])] }
    const line = OV.sumSeries(OV.readTrends(raw))
    assert.deepEqual(line.map((p) => p.value), [30, 32, 34], 'dimensions are summed per day')
    assert.equal(OV.canDrawTrend(line), true)
    assert.equal(OV.canDrawTrend(line.slice(0, 2)), false, 'two days: no line, no placeholder')
    assert.equal(OV.canDrawTrend([]), false)
    assert.equal(OV.canDrawTrend([{ day: 'a', value: 1 }, { day: 'a', value: 2 }, { day: 'a', value: 3 }]), false, 'three points on one day are one day')
    // Defensive reading: restricted, empty, malformed and alternative shapes never throw and never draw.
    for (const bad of [null, undefined, 5, 'x', [], {}, { restricted: true, series: [series('a', [1, 2, 3])] }, { series: 'nope' }, { series: [null, 7] }]) assert.equal(OV.readTrends(bad), null, JSON.stringify(bad))
    assert.deepEqual(OV.sumSeries(null), [])
    assert.deepEqual(OV.readTrends({ series: [{ metric: 'm', dimension: 'd', points: [['2026-10-01', 4], { date: '2026-10-02', v: 5 }, { day: 'x' }, { day: '2026-10-03', value: 'NaN' }] }] })[0].points, [{ day: '2026-10-01', value: 4 }, { day: '2026-10-02', value: 5 }])
    assert.deepEqual(OV.sumSeries(OV.readTrends(raw), ['server']).map((p) => p.value), [10, 11, 12], 'one dimension only')
    // Fresh share per day.
    const cov = { series: [series('fresh', [90, 80, 70]), series('aging', [5, 10, 10]), series('stale', [3, 5, 10]), series('never', [2, 5, 10])] }
    assert.deepEqual(OV.freshShareSeries(OV.readTrends(cov)).map((p) => p.value), [90, 80, 70])
    assert.equal(OV.trendSummary('Assets', line), 'Assets over 3 days: from 30 to 34')
    // The real response (server/app/schemas/inventory.py TrendsResponse): the owner's display switch and today's partial change counts.
    const real = { generated_at: 'x', restricted: false, enabled: true, metric: 'changes', dimension: null, days: 5, window: { since: '2026-10-01', until: '2026-10-05' }, first_day: '2026-10-01', days_with_data: 5, dimensions_total: 1, truncated: false, series: [{ dimension: 'software/changed', points: [1, 2, 3, 4, 5].map((i) => ({ day: `2026-10-0${i}`, value: i * 10 })) }] }
    assert.equal(OV.readTrends({ ...real, enabled: false }), null, 'trends switched off by the owner: no line')
    assert.deepEqual(OV.sumSeries(OV.readTrends(real)).map((p) => p.value), [10, 20, 30, 40, 50])
    assert.equal(OV.trendUntil(real), '2026-10-05'); assert.equal(OV.trendUntil({}), null); assert.equal(OV.trendUntil(null), null)
    assert.deepEqual(OV.withoutDay(OV.sumSeries(OV.readTrends(real)), OV.trendUntil(real)).map((p) => p.value), [10, 20, 30, 40], "today's change count is still being written: the line stops at yesterday")
    assert.equal(OV.withoutDay(OV.sumSeries(OV.readTrends(real)), null).length, 5)
  }

  /* ── change wording (the Overview's recent changes and the flyout) ───────────────────────── */
  {
    const t = (row) => CM.changeText(row)
    const byId = (id) => changes.items.find((r) => r.id === id)
    assert.equal(t(byId(58)).sentence, 'Google Chrome upgraded 151.0.7922.110 → 152.0.8001.12')
    assert.equal(t(byId(57)).sentence, 'FortiOS changed 7.2.5 → 7.2.8', 'a firmware change is "changed", an upgrade only when the server could order the versions')
    assert.equal(t(byId(56)).sentence, 'CrowdStrike Falcon Sensor installed 7.15.18513')
    assert.equal(t(byId(52)).sentence, 'Java 8 Update 391 removed 8.0.3910.11 (part of an upgrade)')
    const base = { category: 'software', action: 'changed', subject: 'X', old_value: '1', new_value: '2', label: null, marker: false, pair: null }
    assert.equal(t(base).verb, 'changed'); assert.equal(t({ ...base, label: 'downgrade' }).verb, 'downgraded')
    assert.equal(t({ ...base, old_value: null }).sentence, 'X changed to 2'); assert.equal(t({ ...base, new_value: '  ' }).sentence, 'X changed from 1')
    assert.equal(t({ ...base, category: 'hardware', subject: 'hw_serial', old_value: 'A', new_value: 'B' }).sentence, 'Serial number changed A → B')
    assert.equal(t({ ...base, category: 'os', subject: 'os_version' }).subject, 'OS version')
    assert.equal(t({ ...base, marker: true }).marker, true)
    assert.match(t({ ...base, marker: true }).sentence, /^Change budget exceeded/)
    // The asset a change links to: a deleted asset is text; a server's SNMP twin opens the server.
    assert.equal(CM.changeAssetAddress({ asset_present: false, asset_kind: 'server', asset_id: 'a', server_id: null }), null)
    assert.equal(CM.changeAssetAddress({ asset_present: true, asset_kind: 'device', asset_id: 'd', server_id: 's' }), 'server:s')
    assert.equal(CM.changeAssetAddress({ asset_present: true, asset_kind: 'device', asset_id: 'd', server_id: null }), 'device:d')
  }

  /* ── the Assets page's logic ─────────────────────────────────────────────────────────────── */
  {
    // Columns and the per-user choice: hostile storage is never trusted.
    assert.deepEqual([...AM.DEFAULT_COLUMNS], ['name', 'ip', 'vendor', 'model', 'os', 'inventory'])
    assert(AM.ASSET_COLUMNS.every((c) => c.sort === null || F.ASSET_SORT_KEYS.includes(c.sort)), 'every sortable column is a sort key the API accepts')
    const d = AM.parsePrefs(null)
    assert.deepEqual([d.columns, d.density, d.facets], [AM.DEFAULT_COLUMNS, 'comfortable', true])
    for (const raw of ['', '{', 'null', '[]', '5', '{"columns":"x"}', undefined, 7]) assert.deepEqual(AM.parsePrefs(raw).columns, [...AM.DEFAULT_COLUMNS], String(raw))
    assert.deepEqual(AM.parsePrefs('{"columns":["serial","bogus","kind"],"density":"compact","facets":false}'), { columns: ['name', 'kind', 'serial'], density: 'compact', facets: false }, 'unknown ids dropped, the name column is always there, table order, not tick order')
    assert.equal(AM.parsePrefs({ columns: [], density: 'huge' }).density, 'comfortable')
    assert.deepEqual(AM.parsePrefs({ columns: [] }).columns, ['name'])
    const toggled = AM.toggleColumn(d, 'serial')
    assert.deepEqual(toggled.columns, ['name', 'ip', 'vendor', 'model', 'os', 'serial', 'inventory'], 'a new column lands in table order')
    assert.deepEqual(AM.toggleColumn(toggled, 'serial').columns, d.columns)
    assert.deepEqual(AM.toggleColumn(d, 'name').columns, d.columns, 'the name column cannot be hidden: it opens the flyout')
    assert.deepEqual(AM.toggleColumn(d, 'nope').columns, d.columns)
    assert.deepEqual(AM.parsePrefs(AM.serializePrefs(toggled)), toggled, 'a stored choice reads back')
    assert.deepEqual(AM.visibleColumns(toggled).map((c) => c.id), toggled.columns)
    assert.notEqual(AM.prefsKey('a'), AM.prefsKey('b'), 'one key per user')
    assert.equal(AM.prefsKey(null), 'zp-inventory-assets-view:anonymous')

    // One row's OS and firmware.
    const row = (name) => assetList.items.find((r) => r.name === name)
    assert.equal(AM.osCell({ kind: 'server', os_family: 'windows', os_product: 'Windows Server', os_release: '2019', os_version: '10.0.17763.5696', os_edition: null }).label, 'Windows Server 2019')
    assert.deepEqual(AM.osCell(row('fg-01')), { label: 'FortiOS', version: '7.2.8', unknown: false, title: 'FortiOS (7.2.8)' })
    assert.deepEqual(AM.osCell(row('edge-unk-01')), { label: 'Unknown', version: null, unknown: true, title: 'Operating system or firmware not identified' })
    assert.equal(AM.osCell({ kind: 'device', os_family: 'other', os_product: null, os_release: null, os_version: null, os_edition: null }).label, 'Other', 'a known family without a product is shown by its family')
    assert.equal(AM.osCell({ kind: 'device', os_family: 'ios-xe', os_product: 'IOS XE', os_release: '17.9', os_version: null, os_edition: null }).version, '17.9')
    assert.equal(AM.rowSummary(row('fg-01')), 'fg-01, network device, 10.20.0.10')
    // Facet groups from the real response: labels in words, fixed vocabularies keep the API order, unknown is never behind "show more".
    const groups = AM.facetGroups(facets)
    assert.deepEqual(groups.map((g) => g.key), ['kind', 'os_family', 'os_product', 'vendor', 'model', 'site', 'tag', 'source', 'freshness', 'health'])
    assert(groups.every((g) => F.ASSET_FILTER_KEYS.includes(g.key)), 'every facet group is a filter of the vocabulary')
    const g = Object.fromEntries(groups.map((x) => [x.key, x]))
    assert.deepEqual(g.kind.items.map((i) => [i.value, i.label]), [['server', 'Server'], ['device', 'Network device']])
    assert.deepEqual(g.freshness.items.map((i) => i.label), ['Fresh', 'Aging', 'Stale', 'Never reported', 'Not reported by design'], 'X3: the fifth state is listed, in words')
    assert(g.freshness.keepOrder && g.kind.keepOrder && g.health.keepOrder && !g.vendor.keepOrder)
    assert.equal(g.health.items[1].label, 'Missing model')
    assert.equal(g.source.items[0].label, 'SNMP')
    assert.equal(g.tag.multi, true)
    assert.deepEqual(g.os_family.unknownPatch, { os_family: 'unknown' })
    assert(g.os_family.limit >= g.os_family.items.length, 'a short list with an unknown row shows all of it')
    assert.equal(g.os_family.items.find((i) => i.unknown).count, 17)
    assert.equal(g.vendor.more, 0)
    assert.equal(AM.facetGroups({ ...facets, facets: { ...facets.facets, vendor: { distinct: 80, items: facets.facets.vendor.items } } }).find((x) => x.key === 'vendor').more, 80 - facets.facets.vendor.items.length, 'values beyond the API cap are announced')
    assert(AM.facetGroups(undefined).every((x) => x.items.length === 0), 'before the facets load there are no items, not a crash')

    // Paging past the end is not clamped by the URL or the API, so the page handles it.
    assert.deepEqual(AM.pageState(2, 145, 50), { pageCount: 3, beyond: false, lastPage: 3 })
    assert.deepEqual(AM.pageState(4, 145, 50), { pageCount: 3, beyond: true, lastPage: 3 })
    assert.equal(AM.pageState(1, 0, 50).beyond, false)
    assert.equal(AM.pageState(9, 0, 50).beyond, false, 'an empty result is the "nothing matches" state, not "past the end"')
    assert.equal(AM.pageState(3, 100, 50).beyond, true)
    assert.equal(AM.rangeText(3, 50, 145), '101 to 145 of 145 assets')
    assert.equal(AM.rangeText(1, 50, 0), '0 assets')
    // Keyboard: arrows, Home, End, paging keys; other keys are left alone.
    assert.deepEqual(['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp', 'Enter'].map((k) => AM.nextRowIndex(5, k, 50)), [6, 4, 0, 49, 15, 0, null])
    assert.equal(AM.nextRowIndex(49, 'ArrowDown', 50), 49); assert.equal(AM.nextRowIndex(0, 'ArrowUp', 50), 0); assert.equal(AM.nextRowIndex(0, 'ArrowDown', 0), null)

    // Flyout links follow the permissions of the pages they open.
    const detail = G('asset-server.json')
    const can = (...p) => (x) => p.includes(x)
    assert.deepEqual(AM.flyoutLinks(detail, can('servers.view', 'devices.view')).map((l) => [l.id, l.to]), [['server', `/servers/${detail.links.server_id}`], ['device', `/devices/${detail.links.device_id}`]])
    const onlyServers = AM.flyoutLinks(detail, can('servers.view'))
    assert.deepEqual(onlyServers.map((l) => [l.id, l.to, l.reason]), [['server', `/servers/${detail.links.server_id}`, null], ['device', null, 'Needs the devices.view permission']])
    const none = AM.flyoutLinks(detail, can())
    assert(none.every((l) => l.to === null && /Needs the (servers|devices)\.view permission/.test(l.reason)), 'without the permission an entry is text with the reason, never a link into a "no access" screen')
    assert.equal(AM.flyoutLinks(G('asset-device.json'), can('devices.view')).length, 1, 'a plain device has one page')
    assert.equal(AM.flyoutLinks(G('asset-device.json'), can('devices.view'))[0].to, `/devices/${G('asset-device.json').links.device_id}`)
    assert.equal(AM.flyoutLinks(G('asset-device.json'), can('servers.view'))[0].to, null, 'servers.view does not open a device page')
    assert.equal(AM.flyoutLinks(detail, can('servers.view'))[1].label, 'Open SNMP device page')
    const addr = 'device:3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
    assert.equal(AM.assetChangesHref(addr), '/inventory/changes?asset=device%3A3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f')
    // FE-05: the flyout counts every event of the asset, so the link carries since = its oldest event and lands on the same total.
    assert.equal(AM.assetChangesHref(addr, '2026-08-14T09:30:00Z'), '/inventory/changes?asset=device%3A3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f&since=2026-08-14T09%3A30%3A00Z')
    assert.equal(AM.assetChangesHref(addr, null), AM.assetChangesHref(addr), 'an asset with no event carries no since')
    assert.equal(AM.serverSoftwareHref('abc'), '/servers/abc?tab=system&sub=software')
    // Values.
    assert.equal(AM.memoryText(8589934592), '8 GB'); assert.equal(AM.memoryText(1.5 * 1024 ** 3), '1.5 GB'); assert.equal(AM.memoryText(2 * 1024 ** 4), '2 TB'); assert.equal(AM.memoryText(null), null); assert.equal(AM.memoryText(0), null)
    assert.equal(AM.disksText([{ model: 'Disk 0', size_bytes: 480103981056 }, { model: 'Disk 1', size_bytes: 960207962112 }]), '2 disks: 447 GB, 894 GB')
    assert.equal(AM.disksText([{}]), '1 disk'); assert.equal(AM.disksText([]), null); assert.equal(AM.disksText(null), null)
    assert.equal(AM.cpuText('AMD EPYC 7543 32-Core Processor', 8, 4), 'AMD EPYC 7543 32-Core Processor · 4 cores, 8 threads')
    assert.equal(AM.cpuText(null, null, null), null)
    assert.deepEqual(['present', 'missing', 'unknown'], [AM.securityAgentState({ present: true }), AM.securityAgentState({ present: false }), AM.securityAgentState({ present: null })])
    assert.equal(AM.identityText({ value: '  ' }), null); assert.equal(AM.identityText({ value: null }), null)
    assert.deepEqual(['serial', 'os_version', 'os_build', 'vendor'].map(AM.isMonoField), [true, true, true, false])
    assert.deepEqual(AM.exportStartedText(2410), { title: 'Export started', description: '2,410 rows as CSV', truncated: false })
    assert.equal(AM.exportStartedText(10, { rows: 7 }).description, '7 rows as CSV', "the server's own count wins")
    assert.equal(AM.exportStartedText(1).description, '1 row as CSV')
    const cut = AM.exportStartedText(250000, { rows: 200000, truncated: true })
    assert(cut.truncated && cut.description.includes('200,000 of 250,000') && /row limit/.test(cut.title), 'a capped file says so')
    assert.deepEqual(['entity-mib', 'snmp', null].map(AM.badgeSource), ['entity', 'snmp', null], 'the API names the ENTITY-MIB badge entity-mib')
  }

  /* ── the compile-time conformance file (the strict build fails when the types and the contract drift) ── */
  {
    const file = read('src/pages/inventory/contract.typecheck.ts')
    // The fixtures of the responses this file's types describe (the software and settings responses have their own types and checks).
    const mine = ['asset-device-ha', 'asset-device', 'asset-server', 'assets-scoped', 'assets', 'changes-scoped', 'changes', 'facets', 'hardware', 'os', 'summary-scoped', 'summary', 'trends-scoped', 'trends']
    for (const f of mine.map((n) => `${n}.json`)) assert(file.includes(`./fixtures/${f}'`), `contract.typecheck.ts checks ${f} against types.ts`)
    assert.equal((file.match(/^\/\/ @ts-expect-error/gm) ?? []).length, 4, 'four negative controls prove the checks are not vacuous')
    assert(/AssertNever<Extra</.test(file) && /conforms</.test(file), 'both directions: no missing field, no extra field')
    // And the build includes it: it is under src/ (tsconfig.app.json include), and nothing imports it at run time.
    assert(/"include":\s*\["src"\]/.test(read('tsconfig.app.json')))
  }

  /* ── the components, rendered from the golden responses ──────────────────────────────────── */
  {
    const qc = (entries) => {
      const client = new m.QueryClient({ defaultOptions: { queries: { retry: false } } })
      for (const [key, value] of entries) client.setQueryData(key, value)
      return client
    }
    const page = (el, client, url = '/inventory') => m.renderToStaticMarkup(h(m.QueryClientProvider, { client }, h(m.MemoryRouter, { initialEntries: [url] }, el)))
    const ALL = ['inventory.view', 'servers.view', 'devices.view', 'discovery.view']
    const seeded = (s = summary) => qc([
      [['inventory', 'summary'], s], [['inventory', 'os', 'server'], osTree], [['inventory', 'os', 'device'], osTree], [['inventory', 'hardware'], hardware],
      [['inventory', 'changes', { page_size: 8 }], changes],
    ])

    // The Overview.
    setUser(USER(ALL))
    const html = page(h(m.InventoryOverviewPage), seeded())
    noNested(html)
    const cards = [...html.matchAll(/data-kpi="(\w+)"/g)].map((x) => x[1])
    assert.deepEqual(cards, ['assets', 'os', 'software', 'hardware', 'fresh', 'changes'])
    const links = anchors(html)
    assert(links.length > 80, `the Overview is made of links (${links.length})`)
    const assetLinks = links.filter((l) => l.attrs['data-drill'] === 'assets')
    for (const l of assetLinks) {
      assert(l.href.startsWith('/inventory/assets'), l.href)
      const parsed = F.parseAssetFilters(new URL(l.href, 'http://x').searchParams)
      assert.deepEqual(parsed.rejected, [], l.href)
      assert.equal(F.assetsHref(parsed.filters), l.href, 'the link is the canonical URL of its filters')
    }
    // Every number that stands for a list says how many: the drill-count attribute.
    const counted = links.filter((l) => l.attrs['data-drill-count'] !== undefined)
    assert(counted.length > 60 && counted.every((l) => /^\d+$/.test(l.attrs['data-drill-count'])))
    // Specific, well-known drills.
    const href = (needle) => links.filter((l) => l.href === needle)
    assert(href('/inventory/assets').length >= 1 && href('/inventory/assets?kind=server').length >= 1 && href('/inventory/assets?kind=device').length >= 1)
    assert(href('/inventory/assets?security_agent=missing').length >= 1 && href('/inventory/assets?security_agent=present').length >= 1 && href('/inventory/assets?security_agent=unknown').length >= 1, 'the endpoint-protection line: covered, gap and unknown')
    assert(href('/inventory/assets?os_family=unknown&kind=server').length >= 1 || href('/inventory/assets?kind=server&os_family=unknown').length >= 1, 'unknown servers are a drillable slice and row')
    assert(links.some((l) => l.href === '/inventory/software/google-chrome'), 'a product opens its page')
    assert(links.some((l) => l.href.startsWith('/inventory/assets?product=google-chrome') && l.attrs['data-drill-count'] === String(summary.software.top.find((p) => p.product_key === 'google-chrome').hosts)), 'hosts open the assets that have the product')
    for (const k of F.HEALTH_PREDICATES) assert(summary.health[k] === 0 || links.some((l) => l.href === `/inventory/assets?health=${k}`), `health item ${k}`)
    assert(links.some((l) => l.href === '/inventory/assets?freshness=not_applicable' && l.attrs['data-drill-count'] === String(summary.health.not_collected_by_design.count)), 'UP-04: the by-design line')
    assert(links.some((l) => l.href === '/inventory/changes'.concat(`?since=${encodeURIComponent(summary.changes_7d.since)}`)), 'view all changes keeps the window')
    assert(links.some((l) => l.href.startsWith('/inventory/assets?asset=server%3A') || l.href.startsWith('/inventory/assets?asset=device%3A')), 'a change links to its asset flyout')
    // The text.
    const text = decode(html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
    const sac = summary.software.security_agent_coverage
    assert(text.includes(`Endpoint protection on ${sac.covered} of ${sac.covered + sac.missing} servers with a complete software list`), 'endpoint-protection coverage line: N of M says what M is (UI-01, UP-01)')
    assert(text.includes(`${sac.unknown} servers unknown: no complete software or service list to judge`), 'unknown servers are their own line')
    assert(!/EDR\/AV|security agent/i.test(text), 'no EDR/AV or "security agent" wording anywhere on the page')
    // FV1-N2: a category is its label ("Endpoint protection"), never its slug; UI-15: both numbers of a row name the product.
    const categories = [...html.matchAll(/data-software-category="">([^<]*)</g)].map((x) => x[1])
    assert(categories.length >= 6 && categories.every((c) => /^[A-Z]/.test(c)), `FV1-N2: categories are labels (${categories})`)
    assert(categories.includes('Endpoint protection') && categories.includes('Remote access'))
    for (const [rows, caption] of [[summary.software.top, 'Most widely installed software, by number of servers'], [summary.software.sprawl, 'Products installed in the most distinct versions']]) {
      const table = html.match(new RegExp(`<table[^>]*>(?:(?!</table>).)*<caption class="sr-only">${caption}(?:(?!</table>).)*</table>`, 's'))[0]
      const row = rows.find((p) => /^[\w -]+$/.test(p.name))
      assert(table.includes(`aria-label="${row.name}: ${M.plural(row.hosts, 'host')}"`) && table.includes(`aria-label="${row.name}: ${M.plural(row.versions, 'version')}"`), `UI-15: in "${caption}", ${row.name} has a named Hosts link and a named Versions link`)
    }
    // UI-19: one is "1 model", "1 vendor", "1 title", "1 server", never "1 models".
    {
      const one = structuredClone(summary)
      one.hardware.models = 1
      one.hardware.vendors = 1
      one.software.products = 1
      const oneText = decode(page(h(m.InventoryOverviewPage), seeded(one)).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
      assert(oneText.includes('1 model from 1 vendor') && oneText.includes('All 1 title'), 'UI-19: the Overview agrees nouns with numbers')
      for (const f of ['OverviewPanels', 'HardwarePanels']) {
        assert(!/\$?\{fmtInt\([^}]*\)\}\)? (servers|devices|releases|models|vendors|titles|products)\b/.test(read(`src/components/inventory/${f}.tsx`)), `UI-19: ${f} counts nouns through plural()`)
      }
    }
    assert(text.includes('Ranked by how many different versions are installed, a sign of upgrade drift. Open a product to see how many hosts are behind the newest version.'), 'UI-06: the version-sprawl note')
    assert(text.includes('Operating systems: servers') && text.includes('Network OS and firmware') && text.includes('Top software') && text.includes('Version sprawl') && text.includes('Hardware') && text.includes('Recent changes') && text.includes('Inventory health'))
    assert(text.includes('Not counted: Update, Driver, OS component, Runtime entries'), 'the hidden categories are stated, not toggled')
    assert(html.includes('<table') && html.includes('scope="col"') && html.includes('scope="row"'), 'real tables with scope headers')
    assert(!html.includes('data-trend-days'), 'no sparkline without trend data')
    assert(!html.includes('data-trends-note'), 'an unrestricted user is told nothing about trends')
    assert(!/Inventory is complete/.test(text))
    // Unknown is drawn as a pattern as well as a word.
    assert(html.includes('repeating-linear-gradient') && html.includes('<pattern'), 'a hatch pattern in the legend swatch and in the donut')
    // Sparklines appear with at least three days of snapshots, and only then.
    const withTrend = seeded()
    const day = (i) => `2026-10-0${i}`
    withTrend.setQueryData(['inventory', 'trends', 'assets'], { restricted: false, series: [{ metric: 'assets', dimension: 'server', points: [1, 2, 3, 4].map((i) => ({ day: day(i), value: 50 + i })) }, { metric: 'assets', dimension: 'device', points: [1, 2, 3, 4].map((i) => ({ day: day(i), value: 80 + i })) }] })
    const t3 = page(h(m.InventoryOverviewPage), withTrend)
    assert.equal((t3.match(/data-trend-days="4"/g) ?? []).length, 1, 'one sparkline: the Assets card')
    assert(decode(t3).includes('Assets over 4 days: from 132 to 138'))
    const short = seeded(); short.setQueryData(['inventory', 'trends', 'assets'], { restricted: false, series: [{ metric: 'assets', dimension: 'server', points: [1, 2].map((i) => ({ day: day(i), value: i })) }] })
    assert(!page(h(m.InventoryOverviewPage), short).includes('data-trend-days'), 'two days of history draw nothing')
    // A scoped user: scoped numbers, no sparkline, and the reason.
    const scoped = page(h(m.InventoryOverviewPage), (() => { const c = seeded(scopedSummary); c.setQueryData(['inventory', 'trends', 'assets'], { restricted: false, series: [{ metric: 'assets', dimension: 'server', points: [1, 2, 3].map((i) => ({ day: day(i), value: i })) }] }); return c })())
    assert(scoped.includes('data-trends-note') && !scoped.includes('data-trend-days'), 'trend lines are never shown to a scoped user')
    assert(decode(scoped).includes(`>${scopedSummary.assets.total}<`) || decode(scoped).includes(String(scopedSummary.assets.total)))
    // Nothing yet: onboarding, with links only where the user may follow them.
    const none = { ...summary, assets: { total: 0, server: 0, device: 0, linked_devices: 0 }, coverage: { ...summary.coverage, fresh: 0, aging: 0, stale: 0, never: 0, fresh_pct: null }, health: Object.fromEntries(F.HEALTH_PREDICATES.map((k) => [k, 0])) }
    const first = page(h(m.InventoryOverviewPage), seeded(none))
    assert(first.includes('Install the Windows agent') && first.includes('Add network devices') && first.includes('Inventory fills in automatically'))
    assert(!first.includes('data-kpi'), 'no empty KPI cards before there is anything to count')
    assert(anchors(first).some((l) => l.href === '/server-agents') && anchors(first).some((l) => l.href === '/discovery'))
    setUser(USER(['inventory.view']))
    const restrictedFirst = page(h(m.InventoryOverviewPage), seeded(none))
    assert(!anchors(restrictedFirst).some((l) => l.href === '/server-agents' || l.href === '/discovery'), 'no link to a page the user may not open')
    assert(restrictedFirst.includes('Needs the servers.view permission') && restrictedFirst.includes('Needs the discovery.view permission'))
    assert(page(h(m.InventoryOverviewPage), seeded({ ...none, scope: { restricted: true, tags: ['team-edge'] } })).includes('No assets match your scope'), 'a scope that matches nothing is not "install the agent"')
    // The all-clear state.
    setUser(USER(ALL))
    const clear = page(h(m.InventoryOverviewPage), seeded({ ...summary, health: Object.fromEntries(F.HEALTH_PREDICATES.map((k) => [k, 0])) }))
    assert(decode(clear).includes('Inventory is complete') && clear.includes('data-health="clear"') && clear.includes('success-fg'))
    // A failed refresh with data in the cache: the last figures stay, and the page says so once (not in every summary panel).
    const http500 = () => Object.assign(new Error('Request failed with status code 500'), { response: { status: 500, data: {} } })
    const stale = seeded()
    const entry = stale.getQueryCache().find({ queryKey: ['inventory', 'summary'] })
    entry.setState({ ...entry.state, status: 'error', error: http500(), fetchStatus: 'idle', errorUpdateCount: 1, fetchFailureCount: 1 })
    const staleHtml = page(h(m.InventoryOverviewPage), stale)
    assert.equal((staleHtml.match(/Showing the last data that loaded/g) ?? []).length, 1, 'one stale-data banner for the shared summary')
    assert(staleHtml.includes('data-kpi="assets"') && staleHtml.includes('Top software') && staleHtml.includes('Endpoint protection on'), 'the last figures stay on screen')
    // A summary that never loaded: the failure is stated with a retry, each panel that needs it fails on its own, and the panels with their own request still render.
    // retryOnMount off: a first load that failed stays failed in the markup (on a real mount it would be tried again).
    const dead = new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
    dead.setQueryData(['inventory', 'os', 'server'], osTree)
    dead.getQueryCache().build(dead, { queryKey: ['inventory', 'summary'] }).setState({ data: undefined, status: 'error', error: http500(), fetchStatus: 'idle', dataUpdatedAt: 0, errorUpdatedAt: Date.now(), errorUpdateCount: 1 })
    const deadHtml = page(h(m.InventoryOverviewPage), dead)
    assert(decode(deadHtml).includes('Could not load the inventory summary'), 'no summary: the failure is stated, never an empty page')
    assert(deadHtml.includes('Windows Server 2022') && deadHtml.includes('Top software') && deadHtml.includes('Inventory health'), 'one failing request does not blank the page')
    assert((deadHtml.match(/role="alert"/g) ?? []).length >= 5, 'the KPI row and each panel that needs the summary say so, with their own retry')
    assert((deadHtml.match(/Retry/g) ?? []).length >= 5)
    // Loading and failure: the page renders without data and says so, per panel.
    const loading = page(h(m.InventoryOverviewPage), qc([]))
    assert(loading.includes('aria-busy="true"') && !loading.includes('data-kpi'), 'skeletons while the summary loads')
    // Without inventory.view nothing renders (the layout shows the denied state).
    setUser(USER(['devices.view']))
    assert.equal(page(h(m.InventoryOverviewPage), seeded()), '')
    setUser(USER(ALL))

    // One KPI card: the card is one link, the figures are links above it, nothing nested.
    const kpi = render(h(m.InventoryKpiCard, { model: OV.kpiModels(summary)[0], icon: h('i'), accent: 'primary' }))
    noNested(kpi)
    assert.equal(anchors(kpi).length, 3)
    assert(kpi.includes("after:absolute") && kpi.includes('aria-label="Assets: 145"'))
    assert.deepEqual(anchors(kpi).map((l) => l.attrs['data-drill-count']), ['145', '60', '85'])

    // The Assets table.
    const cols = AM.visibleColumns(AM.parsePrefs(null))
    const tableProps = { rows: assetList.items.slice(0, 12), columns: cols, density: 'comfortable', sort: 'name', order: 'asc', onSort() {}, onOpen() {}, hrefFor: (a) => `/inventory/assets?asset=${encodeURIComponent(a)}`, activeAddress: null, now: NOW, caption: 'Assets' }
    const table = render(h(m.AssetTable, tableProps))
    noNested(table)
    assert((table.match(/scope="col"/g) ?? []).length === cols.length, 'a header cell with scope=col per column')
    assert((table.match(/scope="row"/g) ?? []).length === 12, 'the name is the row header')
    assert(table.includes('aria-sort="ascending"') && (table.match(/aria-sort="none"/g) ?? []).length === cols.filter((c) => c.sort).length - 1)
    assert.equal((table.match(/tabindex="0"/g) ?? []).length, 1, 'one tab stop for the whole table (roving)')
    assert.equal((table.match(/tabindex="-1"/g) ?? []).length, 11)
    const rowLinks = anchors(table)
    assert.equal(rowLinks.length, 12)
    assert(rowLinks.every((l) => l.attrs['data-asset-link'] && l.href === `/inventory/assets?asset=${encodeURIComponent(l.attrs['data-asset-link'])}`), 'every row is addressed <kind>:<id> and opens its flyout')
    assert(rowLinks.every((l) => F.parseAssetAddress(l.attrs['data-asset-link'])), 'the address is the asset vocabulary')
    assert(decode(table).includes('aria-label="edge-unk-01, network device, 10.20.3.12"'))
    assert(table.includes('Unknown') && table.includes('data-freshness="never"'), 'unknown is a word, not a blank')
    assert(table.includes('data-freshness="fresh"') && table.includes('data-freshness="stale"'))
    const selected = render(h(m.AssetTable, { ...tableProps, activeAddress: tableProps.rows[1].key }))
    assert.equal((selected.match(/aria-selected="true"/g) ?? []).length, 1)
    const all = AM.ASSET_COLUMNS.map((c) => c.id)
    const wideTable = render(h(m.AssetTable, { ...tableProps, columns: AM.visibleColumns({ columns: all, density: 'compact', facets: true }), density: 'compact' }))
    for (const c of AM.ASSET_COLUMNS) assert(wideTable.includes(`>${c.label}`), `column ${c.id}`)
    assert(wideTable.includes('data-source="snmp"'))
    // The page without rows renders its header only; a sort header is a button.
    assert(table.includes('<button') && table.includes('title="Sort by Name, now ascending"'))

    // The toolbar: Export exists only for inventory.export; saved views are Phase 2 and have no button.
    const bar = (canExport) => render(h(m.AssetToolbar, {
      q: '', onQ() {}, chips: F.activeFilterChips({ kind: 'device', tag: ['pci', 'edge'], q: 'x' }), hrefWith: () => '/x', onClear() {}, filtersOpen: true, onToggleFilters() {},
      wide: true, filterCount: 3, columns: [...AM.DEFAULT_COLUMNS], onToggleColumn() {}, density: 'comfortable', onDensity() {}, canExport, onExport() {}, exporting: false, total: 145, loading: false,
    }))
    assert(!bar(false).includes('Export CSV') && bar(true).includes('Export CSV'))
    assert(!/saved view/i.test(bar(true)), 'no saved-views button before Phase 2')
    const b = bar(true)
    assert(b.includes('aria-label="Search assets"') && b.includes('aria-keyshortcuts="/"'))
    assert.equal((b.match(/aria-label="Remove filter/g) ?? []).length, 3, 'a removable chip per active filter (the search box shows q itself)')
    assert(b.includes('Clear all') && b.includes('145') && b.includes('Hide filters'))
    assert(render(h(m.AssetToolbar, { q: '', onQ() {}, chips: [], hrefWith: () => '/x', onClear() {}, filtersOpen: false, onToggleFilters() {}, wide: false, filterCount: 0, columns: [...AM.DEFAULT_COLUMNS], onToggleColumn() {}, density: 'comfortable', onDensity() {}, canExport: false, onExport() {}, exporting: false, total: undefined, loading: true })).includes('aria-haspopup="dialog"'), 'below lg the filter button opens a drawer')

    // The flyout body, from each golden asset.
    const flyout = (file, can) => { setUser(USER(can)); return render(h(m.FlyoutBody, { detail: G(file) })) + render(h(m.FlyoutHeader, { detail: G(file), now: NOW })) }
    const srv = flyout('asset-server.json', ['inventory.view', 'servers.view'])
    noNested(srv)
    const srvText = decode(srv.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
    assert(srvText.includes('Vendor') && srvText.includes('Dell') && srvText.includes('PowerEdge R650') && srvText.includes('DELL00003'))
    assert((srv.match(/data-identity="/g) ?? []).length === 9, 'nine identity fields, always')
    assert(srv.includes('data-source="snmp"') && srv.includes('data-source="agent"'), 'every value carries its source')
    assert(srvText.includes('Endpoint protection present') && srvText.includes('CrowdStrike Falcon Sensor'))
    assert(srv.includes('href="/inventory/software/crowdstrike-falcon-sensor"') && srv.includes('href="/inventory/software/git"'), 'software links to its product page')
    assert(srv.includes(`href="/servers/${G('asset-server.json').links.server_id}"`), 'with servers.view the server page is a link')
    assert(/needs the devices\.view permission/i.test(srvText) && !srv.includes(`href="/devices/`), 'without devices.view the SNMP device page is text with the reason')
    assert(srvText.includes('View all 14 on the server page') && srv.includes('/servers/') && srv.includes('tab=system&amp;sub=software'))
    assert(srvText.includes('Raw evidence') && srvText.includes('Microsoft Windows Server 2019 Standard'))
    assert(srvText.includes('8 GB') && srvText.includes('AMD EPYC 7543') && srvText.includes('3 disks: 447 GB, 894 GB, 1.3 TB'))
    assert(srv.includes('/inventory/changes?asset=server%3Afaa42e3e'), 'recent changes link to the asset history')
    assert(srvText.includes('3 of 3'))
    const srvNoPerm = flyout('asset-server.json', ['inventory.view'])
    assert(!srvNoPerm.includes('href="/servers/') && /needs the servers\.view permission/i.test(decode(srvNoPerm.replace(/<[^>]+>/g, ' '))), 'no servers.view: plain text with an explanation')
    assert(decode(srvNoPerm).includes('it needs the servers.view permission'))
    const dev = flyout('asset-device.json', ['inventory.view', 'devices.view'])
    noNested(dev)
    const devText = decode(dev.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
    assert(devText.includes('Chassis') && devText.includes('C9300-24T') && devText.includes('Power supply') && devText.includes('PWR-C1-715WAC-P') && devText.includes('FCW2312L004') && devText.includes('DCA00025A'))
    assert(dev.includes('<details') && devText.includes('× 2'), 'modules by class with their serials')
    assert(dev.includes(`href="/devices/${G('asset-device.json').links.device_id}"`) && !dev.includes('/servers/'))
    assert(!devText.includes('High availability'))
    const ha = decode(flyout('asset-device-ha.json', ['inventory.view', 'devices.view']).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
    assert(ha.includes('High availability') && ha.includes('Active-Passive') && ha.includes('Primary'))
    assert(ha.includes('No changes recorded for this asset'), 'an asset without events says so and where history starts')
    // A truncated list, a server that was never baselined, an unknown value: each its own honest text.
    const base = G('asset-server.json')
    const variant = (mutate) => { const d = structuredClone(base); mutate(d); setUser(USER(ALL)); return decode(render(h(m.FlyoutBody, { detail: d })).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')) }
    assert(variant((d) => { d.server.software.truncated = true }).includes("The agent's software list was cut at its cap"))
    const un = variant((d) => { d.server.baselined = false; d.server.software.security_agent = { present: null, state: 'unknown', reason: 'no_software_list', products: [], services: [] }; d.server.software.top = []; d.identity[0].value = null; d.identity[0].source = null })
    assert(un.includes('Endpoint protection unknown') && un.includes('No software list has been reported for this server') && un.includes('Unknown'))
    assert(variant((d) => { d.server.software.security_agent = { present: false, state: 'missing', reason: null, products: [], services: [] } }).includes('No endpoint protection found'))
    // X1: unknown is not missing, and each cause is named.
    assert(variant((d) => { d.server.software.security_agent = { present: null, state: 'unknown', reason: 'list_truncated', products: [], services: [] } }).includes('The software list was cut off, so protection cannot be judged'))
    assert(variant((d) => { d.server.software.security_agent = { present: null, state: 'unknown', reason: 'normalising', products: [], services: [] } }).includes('The software list is still being processed'))
    // Built-in Defender counts through its running service.
    assert(variant((d) => { d.server.software.security_agent = { present: true, state: 'present', reason: null, products: [], services: ['WinDefend'] } }).includes('WinDefend service'))
    assert(variant((d) => { d.device = null; d.server.disks = []; d.server.cpu_model = null; d.server.memory_total_bytes = null }).includes('Hardware'))
  }
}

/* ═══ (h) fix round: session cache, search boxes, live URL writes, tz fallback ════════════════ */

// A fake clock for the search state machine: timers fire in order as time advances.
function fakeClock() {
  let now = 0
  let id = 0
  const timers = new Map()
  return {
    scheduler: {
      setTimeout(fn, ms) { const handle = ++id; timers.set(handle, { at: now + ms, fn }); return handle },
      clearTimeout(handle) { timers.delete(handle) },
    },
    advance(ms) {
      const end = now + ms
      for (;;) {
        let next = null
        for (const [handle, t] of timers) if (t.at <= end && (next === null || t.at < next[1].at)) next = [handle, t]
        if (next === null) break
        now = next[1].at
        timers.delete(next[0])
        next[1].fn()
      }
      now = end
    },
    pending: () => timers.size,
  }
}

// FE-02: a keystroke that lands right after the debounce fired must not be dropped by the echo of the commit.
{
  const clock = fakeClock()
  const commits = []
  const ctl = SEARCH.createSearchController({ value: '', delay: 300, scheduler: clock.scheduler, onCommit: (q) => commits.push(q) })
  ctl.type('w'); ctl.type('wi'); ctl.type('win')
  clock.advance(299)
  assert.deepEqual(commits, [], 'nothing is committed before the quiet time has passed')
  clock.advance(1)
  assert.deepEqual(commits, ['win'])
  // The user types the next character 20 ms after the debounce fired, before the router has re-rendered.
  clock.advance(20)
  ctl.type('wind')
  // The router now delivers the echo of the committed value.
  ctl.syncUrl('win')
  assert.equal(ctl.getText(), 'wind', 'FE-02: the echo of the box\'s own commit does not overwrite what was typed since')
  clock.advance(300)
  assert.deepEqual(commits, ['win', 'wind'])
  ctl.syncUrl('wind')
  assert.equal(ctl.getText(), 'wind')
  assert.equal(ctl.isPending(), false)
  clock.advance(5000)
  assert.deepEqual(commits, ['win', 'wind'], 'a commit is never repeated')

  // Typing "windows server data" with pauses around the boundary loses nothing, whatever the pauses are.
  for (const gap of [150, 290, 300, 301, 310, 320, 340, 450]) {
    const c = fakeClock()
    const seen = []
    let url = ''
    const box = SEARCH.createSearchController({ value: url, delay: 300, scheduler: c.scheduler, onCommit: (q) => seen.push(q) })
    const text = 'windows server data'
    for (let i = 1; i <= text.length; i++) {
      box.type(text.slice(0, i))
      c.advance(gap)
      // The router re-renders 15 ms after each commit.
      if (seen.length && seen[seen.length - 1] !== url) { c.advance(15); url = seen[seen.length - 1]; box.syncUrl(url) }
    }
    c.advance(1000)
    if (seen.length && seen[seen.length - 1] !== url) { url = seen[seen.length - 1]; box.syncUrl(url) }
    assert.equal(box.getText(), text, `gap ${gap} ms: the box shows exactly what was typed`)
    assert.equal(url, text, `gap ${gap} ms: the address ends with exactly what was typed`)
  }

  // Back, a removed chip or "Clear all" change the address from outside: the box follows and a waiting commit is dropped.
  const c2 = fakeClock()
  const seen2 = []
  const box2 = SEARCH.createSearchController({ value: 'abc', delay: 300, scheduler: c2.scheduler, onCommit: (q) => seen2.push(q) })
  assert.equal(box2.getText(), 'abc')
  box2.type('abcd')
  box2.syncUrl('')
  assert.equal(box2.getText(), '', 'the box follows an address changed from outside')
  assert.equal(box2.isPending(), false)
  c2.advance(1000)
  assert.deepEqual(seen2, [], 'a commit that was waiting when the address changed from outside is dropped')
  box2.syncUrl('xyz')
  assert.equal(box2.getText(), 'xyz', 'Back to an older search')

  // Text equal to what the address already says commits nothing; so does typing and deleting back.
  const c3 = fakeClock()
  const seen3 = []
  const box3 = SEARCH.createSearchController({ value: 'abc', delay: 300, scheduler: c3.scheduler, onCommit: (q) => seen3.push(q) })
  box3.type('abc '); c3.advance(1000)
  box3.type('abcd'); box3.type('abc'); c3.advance(1000)
  assert.deepEqual(seen3, [])
  // Enter commits at once and the timer does not commit it again.
  box3.type('abcz'); box3.flush()
  assert.deepEqual(seen3, ['abcz'])
  c3.advance(1000)
  assert.deepEqual(seen3, ['abcz'])
  // The box's clear button commits the empty search at once; "Clear all" resets the box without committing.
  box3.clear()
  assert.deepEqual(seen3, ['abcz', ''])
  assert.equal(box3.getText(), '')
  box3.type('qq'); box3.reset()
  c3.advance(1000)
  assert.equal(box3.getText(), '')
  assert.deepEqual(seen3, ['abcz', ''], 'reset() drops the waiting commit')

  // White space collapses the way the filters read it: "a  b" comes back from the address as "a b" and is not an outside change.
  const c4 = fakeClock()
  const box4 = SEARCH.createSearchController({ value: '', delay: 300, scheduler: c4.scheduler, onCommit() {} })
  box4.type('a  b'); c4.advance(300); box4.type('a  b c'); box4.syncUrl('a b')
  assert.equal(box4.getText(), 'a  b c')
  // An echo that never comes (a value the page refused) does not wedge the box: the next outside change is followed.
  const c5 = fakeClock()
  const box5 = SEARCH.createSearchController({ value: '', delay: 300, scheduler: c5.scheduler, onCommit() {} })
  for (let i = 0; i < 20; i++) { box5.type(`v${i}`); c5.advance(300) }
  box5.syncUrl('other')
  assert.equal(box5.getText(), 'other')
}

// FE-07: every change is applied to the address AS IT IS, never to the filters of an older render.
{
  const config = U.urlStateConfig(F.assetFilterCodec, {})
  let cur = new URLSearchParams('sort=name&order=asc')
  const writes = []
  let backs = 0
  let locState = null
  const io = {
    read: () => new URLSearchParams(cur),
    write: (next, o) => { cur = next; writes.push(o) },
    back: () => { backs++ },
    locationState: () => locState,
  }
  const w = U.createUrlWriter(config, io)
  // The search box commits q while a facet was clicked in between: both survive, in either order.
  w.patchFilters({ kind: 'server' })
  w.patchFilters({ q: 'srv' })
  assert.deepEqual(F.parseAssetFilters(cur).filters, { q: 'srv', kind: 'server' }, 'FE-07: the debounced q patch does not undo the facet clicked while it waited')
  assert.equal(writes[1].replace, true, 'typing replaces the history entry')
  assert.equal(writes[0].replace, false, 'a facet pushes')
  w.patchFilters({ q: null })
  assert.deepEqual(F.parseAssetFilters(cur).filters, { kind: 'server' })
  // A function receives the live filters.
  w.setFilters((live) => ({ ...live, vendor: 'Cisco' }))
  w.setFilters((live) => ({ ...live, q: 'x' }), { push: false })
  assert.deepEqual(F.parseAssetFilters(cur).filters, { q: 'x', kind: 'server', vendor: 'Cisco' })
  assert.equal(cur.get('sort'), 'name', 'other parameters stay')
  // Tags and clearing.
  w.toggleTag('pci'); w.toggleTag('edge')
  assert.deepEqual(F.parseAssetFilters(cur).filters.tag, ['edge', 'pci'])
  w.clearFilters()
  assert.deepEqual(F.parseAssetFilters(cur).filters, {})
  // FE-09: a flyout opened by a push is closed by going Back; a deep-linked one is closed by replacing the entry.
  w.openAsset('server:11111111-1111-4111-8111-111111111111')
  assert.equal(cur.get('asset'), 'server:11111111-1111-4111-8111-111111111111')
  assert.deepEqual(writes[writes.length - 1], { replace: false, state: U.FLYOUT_STATE }, 'opening pushes and marks the entry')
  locState = U.FLYOUT_STATE
  w.closeAsset()
  assert.equal(backs, 1, 'FE-09: closing a flyout that was opened by a push goes Back, so no dead entry is left')
  locState = null
  const before = writes.length
  w.closeAsset()
  assert.equal(backs, 1)
  assert.equal(writes.length, before + 1)
  assert.equal(cur.has('asset'), false, 'a deep-linked flyout is closed by replacing the entry')
  assert.equal(writes[writes.length - 1].replace, true)
  assert.equal(U.closeAssetPlan({ zpFlyout: true }), 'back')
  assert.equal(U.closeAssetPlan(null), 'replace')
  assert.equal(U.closeAssetPlan({ other: 1 }), 'replace')
}

// FE-01: nothing fetched for one session may be shown to the next.
{
  const id = (n, extra = {}) => ({ id: n, permissions_revision: 'r1', scope_tags: [], role_scope_tags: [], ...extra })
  const sess = (token, user) => ({ token, user })
  const reset = AUTH.shouldResetQueryCache
  assert.equal(reset(sess('t1', id('u1')), sess(null, null)), true, 'sign-out')
  assert.equal(reset(sess('t1', id('u1')), sess('t2', id('u2'))), true, 'another account signs in')
  assert.equal(reset(sess(null, null), sess('t2', id('u2'))), true, 'sign-in after a sign-out')
  assert.equal(reset(sess('t1', id('u1')), sess('t1', null)), true, 'the user is gone')
  assert.equal(reset(sess('t1', id('u1')), sess('t1', id('u1'))), false, 'the same account refreshed (hydrateUser every minute) keeps the cache')
  assert.equal(reset(sess('t1', id('u1')), sess('t9', id('u1'))), false, 'a new token for the same account keeps the cache')
  assert.equal(reset(sess('t1', id('u1')), sess('t1', id('u1', { permissions_revision: 'r2' }))), true, 'the same account\'s access changed')
  assert.equal(reset(sess('t1', id('u1')), sess('t1', id('u1', { scope_tags: ['edge'] }))), true, 'a tag scope was added')
  assert.equal(reset(sess('t1', id('u1', { scope_tags: ['b', 'a'] })), sess('t1', id('u1', { scope_tags: ['a', 'b'] }))), false, 'the order of tags is not a change')
  assert.equal(reset(sess('t1', id('u1', { role_scope_tags: ['x'] })), sess('t1', id('u1', { role_scope_tags: [] }))), true, 'the role\'s scope changed')
  assert.equal(reset(sess('t1', { id: 'u1' }), sess('t1', id('u1'))), false, 'fields the login response did not carry yet are not a difference')

  // Wired to a store the way main.tsx does it, with the app's real auth store.
  let cleared = 0
  const unsubscribe = AUTH.resetQueryCacheOnSessionChange(m.useAuth, { clear: () => { cleared++ } })
  const store = m.useAuth
  store.setState({ token: 'a', user: { id: 'admin', username: 'admin', email: 'a@x', role: 'admin', permissions: ['system.admin'] } })
  assert.equal(cleared, 1, 'login of the first user')
  store.setState({ user: { id: 'admin', username: 'admin', email: 'a@x', role: 'admin', permissions: ['system.admin'], full_name: 'Admin' } })
  assert.equal(cleared, 1, 'profile refresh does not clear')
  store.getState().logout()
  assert.equal(cleared, 2, 'logout (also reached by zp-auth-expired) clears the cache')
  assert.equal(store.getState().token, null)
  store.setState({ token: 'b', user: { id: 'scoped', username: 's', email: 's@x', role: 'viewer', permissions: ['inventory.view'], scope_tags: ['edge'] } })
  assert.equal(cleared, 3, 'a scoped user signing in after an unrestricted one starts with an empty cache')
  store.setState({ token: null, user: null })
  assert.equal(cleared, 4, 'a rejected session (401) clears it as well')
  unsubscribe()
  store.setState({ token: 'c', user: { id: 'another', username: 'o', email: 'o@x', role: 'viewer' } })
  assert.equal(cleared, 4, 'unsubscribed')
  store.setState({ token: null, user: null })
  // main.tsx really wires it.
  const mainSource = read('src/main.tsx')
  assert(/resetQueryCacheOnSessionChange\(useAuth, queryClient\)/.test(mainSource), 'main.tsx subscribes the module-level QueryClient to the auth store')
}

// FE-03: a zone the API refuses never makes the page fail.
{
  const refusal = (detail) => ({ response: { status: 422, data: { detail } } })
  assert.equal(API.isTimeZoneRejection(refusal('tz must be an IANA time zone name')), true)
  assert.equal(API.isTimeZoneRejection(refusal([{ loc: ['query', 'tz'], msg: 'bad' }])), true)
  assert.equal(API.isTimeZoneRejection(refusal('since must be before 2100')), false)
  assert.equal(API.isTimeZoneRejection({ response: { status: 500, data: { detail: 'tz' } } }), false)
  assert.equal(API.isTimeZoneRejection(new Error('network')), false)
  const calls = []
  const ok = await API.withTimeZoneFallback(async (p) => { calls.push({ ...p }); if (p.tz) throw refusal('tz must be an IANA time zone name'); return 'ok' }, { since: 'x', tz: 'Asia/Calcutta', page: 1 })
  assert.equal(ok, 'ok')
  assert.deepEqual(calls, [{ since: 'x', tz: 'Asia/Calcutta', page: 1 }, { since: 'x', page: 1 }], 'retried once without tz')
  // Another failure is not retried; a request without tz is not retried either.
  let n = 0
  await assert.rejects(API.withTimeZoneFallback(async () => { n++; throw refusal('since must be before 2100') }, { tz: 'UTC' }))
  assert.equal(n, 1)
  n = 0
  await assert.rejects(API.withTimeZoneFallback(async () => { n++; throw refusal('tz must be an IANA time zone name') }, { page: 1 }))
  assert.equal(n, 1)
  n = 0
  await assert.rejects(API.withTimeZoneFallback(async () => { n++; throw refusal('tz must be an IANA time zone name') }, { tz: 'Nowhere/Land' }))
  assert.equal(n, 2, 'the retry itself is made once')
}

// FE-04: an exact-match value keeps the white space inside it (the API compares the stored string); only free text collapses.
{
  const cpu = 'Intel(R) Xeon(R) CPU           X5650  @ 2.67GHz'
  const href = F.assetsHref({ cpu_model: cpu })
  assert.equal(F.parseAssetFilters(new URL(href, 'http://x').searchParams).filters.cpu_model, cpu, 'a CPU model with runs of spaces survives the link')
  for (const [key, value] of [['site', 'DR  /  rack 2'], ['model', 'WS-C2960X  48TS'], ['module_model', 'PWR  C1'], ['product_version', '2.0 beta  2'], ['os_product', 'Windows  Server']]) {
    const f = key === 'product_version' ? { product: 'x', product_version: value } : { [key]: value }
    assert.equal(F.parseAssetFilters(new URL(F.assetsHref(f), 'http://x').searchParams).filters[key], value, key)
  }
  assert.deepEqual(F.parseAssetFilters(new URL(F.assetsHref({ tag: ['tag  with  spaces'] }), 'http://x').searchParams).filters.tag, ['tag  with  spaces'], 'a tag too')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ site: '  HQ  ' })).filters.site, 'HQ', 'the ends are trimmed')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ q: 'a   b\t c ' })).filters.q, 'a b c', 'free text collapses')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ site: 'a\tb' })).rejected[0].reason, 'contains control characters', 'a tab inside an exact value is refused, not collapsed')
  // Too long is reported, never dropped without a word; the limits are the API's (FE-11).
  assert.equal(F.parseAssetFilters(new URLSearchParams({ cpu_model: 'x'.repeat(201) })).rejected[0].reason, 'longer than 200 characters')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ product: 'p'.repeat(161) })).rejected[0].reason, 'longer than 160 characters')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ product: 'p'.repeat(160) })).rejected.length, 0)
  assert.equal(F.parseAssetFilters(new URLSearchParams({ source: 's'.repeat(33) })).rejected[0].key, 'source')
  assert.equal(F.parseAssetFilters(new URLSearchParams({ source: 's'.repeat(32) })).rejected.length, 0)
  assert.equal(F.parseAssetFilters(new URLSearchParams({ virtualization: 'v'.repeat(33) })).rejected[0].reason, 'longer than 32 characters')
  // A number whose list cannot be opened exactly is shown as text with the reason, never as a link to a wider list.
  const long = render(h(m.DrillLink, { filters: { cpu_model: 'x'.repeat(201) }, count: 1 }, '1'))
  assert(long.includes('data-drill-unavailable') && !long.includes('<a') && decode(long).includes('longer than 200 characters'), long)
  assert(render(h(m.DrillLink, { filters: { cpu_model: cpu }, count: 1 }, '1')).includes('<a'), 'an ordinary value is still a link')
  // The Changes page: the same rule for the product key, 160 characters like the API.
  assert.equal(CHG.parseChangesFilters(new URLSearchParams({ subject_key: 'a  b' })).filters.subject_key, 'a  b')
  assert.equal(CHG.parseChangesFilters(new URLSearchParams({ subject_key: 'k'.repeat(161) })).rejected[0].reason, 'longer than 160 characters')
  assert.equal(CHG.parseChangesFilters(new URLSearchParams({ q: 'a   b' })).filters.q, 'a b')
  // UI-18: chips say names, not slugs.
  const chips = F.activeFilterChips({ os_family: 'ios-xe', source: 'snmp', product: 'google-chrome' }, { source: M.sourceLabel, product: (k) => (k === 'google-chrome' ? 'Google Chrome' : undefined) })
  assert.deepEqual(chips.map((c) => c.text), ['IOS XE', 'SNMP', 'Google Chrome'])
  assert.equal(F.activeFilterChips({ os_family: 'fortiswitchos' })[0].text, 'FortiSwitchOS')
  assert.equal(F.activeFilterChips({ os_family: 'madeup' })[0].text, 'madeup', 'an unknown slug is shown as it is')
  // DT-07: install sources read as words.
  assert.equal(M.sourceLabel('hklm_wow6432'), 'HKLM (32-bit)')
  assert.equal(M.installSourceLabel('hkcu'), 'HKCU (current user)')
  assert.equal(M.installSourceLabel(''), 'Unknown')
  assert.equal(M.sourceLabel('snmp'), 'SNMP')
  // (k) the share maths of a bar whose hosts can sit in several entries: shares of the DISTINCT hosts, not of the sum.
  const e = (key, count) => ({ key, label: key, count, rank: 0 })
  const sum = M.collapseVersionBuckets([e('a', 8), e('b', 6)])
  assert.equal(sum.total, 14)
  const distinct = M.collapseVersionBuckets([e('a', 8), e('b', 6)], { total: 10 })
  assert.equal(distinct.total, 10)
  assert.equal(distinct.segments[0].pct, 80)
  assert.equal(distinct.segments[1].pct, 60)
}

// FE-09: a row link that opens the flyout marks its history entry, so closing goes Back (checked in a browser: idx 0 -> 1 -> 0).
assert(/state=\{FLYOUT_STATE\}/.test(read('src/components/inventory/AssetTable.tsx')), 'the row link carries the flyout marker')
assert(/closeAssetPlan\(io\.locationState\(\)\)/.test(read('src/pages/inventory/urlState.ts')))

// UI-08: focus after a chip is removed or "Clear all": the hook's decision is plain data, the element is looked up in the DOM.
{
  const toolbar = render(h(m.AssetToolbar, {
    q: '', onQ() {}, chips: F.activeFilterChips({ kind: 'device', tag: ['pci'] }), hrefWith: () => '/x', onClear() {}, filtersOpen: true, onToggleFilters() {},
    wide: true, filterCount: 2, columns: [...AM.DEFAULT_COLUMNS], onToggleColumn() {}, density: 'comfortable', onDensity() {}, canExport: false, onExport() {}, exporting: false, total: 3, loading: false,
  }))
  assert((toolbar.match(/data-chip-remove/g) ?? []).length === 2, 'every remove link is findable for the focus move')
}

/* ═══ (i) round 2: lazy-page recovery, search-box edge cases, chip focus, shares, banner, drill links, trimming ═══ */

// NF-01: a lazy page whose file cannot be downloaded gets a notice whose button really recovers, and one guarded automatic reload.
{
  const chunkErrors = [
    'Failed to fetch dynamically imported module: https://zp.example/assets/InventoryAssetsPage-AbC123.js',
    'error loading dynamically imported module: https://zp.example/assets/a.js',
    'Importing a module script failed.',
    'Failed to load module script: Expected a JavaScript module script but the server responded with a MIME type of "text/html".',
    'Loading chunk 17 failed.',
    'Loading CSS chunk 3 failed.',
    'Unable to preload CSS for /assets/index-1.css',
  ]
  for (const message of chunkErrors) {
    assert(CHUNK.isChunkLoadError(new TypeError(message)), `NF-01: ${message}`)
    assert(CHUNK.isChunkLoadError(message), 'a thrown string as well')
  }
  const named = new Error('x')
  named.name = 'ChunkLoadError'
  assert(CHUNK.isChunkLoadError(named))
  assert(CHUNK.isChunkLoadError(new Error('render failed', { cause: new TypeError(chunkErrors[0]) })), 'a wrapped error is still a chunk error')
  for (const other of [new Error("Cannot read properties of undefined (reading 'map')"), new RangeError('Maximum call stack size exceeded'), null, undefined, 42, {}, 'boom']) {
    assert(!CHUNK.isChunkLoadError(other), `not a chunk error: ${String(other)}`)
  }

  // The guard: one automatic reload per window, never a loop.
  const WINDOW = CHUNK.CHUNK_RELOAD_WINDOW_MS
  const T0 = 1_700_000_000_000
  assert(CHUNK.mayAutoReload(null, T0) && CHUNK.mayAutoReload('', T0) && CHUNK.mayAutoReload('junk', T0), 'no record: allowed')
  assert(!CHUNK.mayAutoReload(String(T0 - 1000), T0), 'a reload a second ago: not again')
  assert(!CHUNK.mayAutoReload(String(T0 - WINDOW + 1), T0))
  assert(CHUNK.mayAutoReload(String(T0 - WINDOW), T0), 'after the window: allowed')
  assert(!CHUNK.mayAutoReload(String(T0 + 1000), T0), 'a clock that moved backwards does not open the gate')

  const memory = () => {
    const data = new Map()
    return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => { data.set(k, v) } }
  }
  const failing = new TypeError(chunkErrors[0])
  const reloads = []
  let storage = memory()
  const io = (now, st = storage) => ({ storage: st, now, reload: () => reloads.push(st ? [...st.data.entries()] : null) })
  assert.equal(CHUNK.recoverFromChunkError(failing, io(T0)), 'reloading')
  assert.equal(reloads.length, 1)
  assert.deepEqual(reloads[0], [[CHUNK.CHUNK_RELOAD_KEY, String(T0)]], 'the guard is written BEFORE the page reloads, so a failure after it cannot loop')
  assert.equal(CHUNK.recoverFromChunkError(failing, io(T0 + 5000)), 'manual', 'the reload did not help: the notice and its button, no second reload')
  assert.equal(reloads.length, 1)
  assert.equal(CHUNK.recoverFromChunkError(failing, io(T0 + WINDOW)), 'reloading', 'an update much later is recovered again')
  assert.equal(reloads.length, 2)
  // A page that keeps failing reloads at most once per window, however often it is asked.
  storage = memory()
  let count = 0
  for (let t = 0; t < 3 * WINDOW; t += 1000) {
    if (CHUNK.recoverFromChunkError(failing, { storage, now: T0 + t, reload: () => { count++ } }) === 'reloading') continue
  }
  assert.equal(count, 3, 'three windows, three reloads at most')
  // Not a chunk error: nothing happens.
  const before = reloads.length
  assert.equal(CHUNK.recoverFromChunkError(new Error('boom'), io(T0 + 10 * WINDOW)), 'not-a-chunk-error')
  assert.equal(reloads.length, before)
  // Storage that cannot hold the guard means no automatic reload (it could not be kept from looping).
  const throwing = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }
  const writeFails = { getItem: () => null, setItem() { throw new Error('quota') } }
  for (const st of [null, throwing, writeFails]) {
    let n = 0
    assert.equal(CHUNK.recoverFromChunkError(failing, { storage: st, now: T0, reload: () => { n++ } }), 'manual')
    assert.equal(n, 0, 'no unguarded reload')
  }

  // The notice: a real button, in both states; the boundary shows it (and not "Try again") for a chunk error.
  const manual = m.renderToStaticMarkup(h(m.ChunkLoadNotice, { reloading: false, onReload() {} }))
  assert(manual.includes('data-chunk-notice="manual"') && manual.includes('role="alert"') && manual.includes('<button') && manual.includes('Reload page'))
  assert(manual.includes('This page could not be loaded') && manual.includes('does not sign you out') && !manual.includes('Try again'))
  const auto = m.renderToStaticMarkup(h(m.ChunkLoadNotice, { reloading: true, onReload() {} }))
  assert(auto.includes('data-chunk-notice="reloading"') && auto.includes('role="status"') && auto.includes('Reload now'))
  const boundary = new m.ErrorBoundary({ children: null })
  boundary.state = { error: failing, recovery: 'manual' }
  const shown = boundary.render()
  const boundaryHtml = m.renderToStaticMarkup(shown)
  assert(boundaryHtml.includes('Reload page') && !boundaryHtml.includes('Try again'), 'NF-01: the chunk error is not offered "Try again"')
  const savedWindow = globalThis.window
  let reloadClicks = 0
  globalThis.window = { location: { reload: () => { reloadClicks++ } } }
  try {
    shown.props.onReload()
  } finally {
    if (savedWindow === undefined) delete globalThis.window
    else globalThis.window = savedWindow
  }
  assert.equal(reloadClicks, 1, 'NF-01: the button reloads the page')
  boundary.state = { error: new Error('plain failure'), recovery: null }
  const plain = m.renderToStaticMarkup(boundary.render())
  assert(plain.includes('Try again') && plain.includes('plain failure') && !plain.includes('Reload page'), 'any other error keeps its message and "Try again"')
  // The Inventory layout keeps its own boundary, so the tabs stay usable when a page fails.
  assert(/<ErrorBoundary resetKey=/.test(read('src/pages/inventory/InventoryLayout.tsx')) && /import \{ ErrorBoundary \}/.test(read('src/pages/inventory/InventoryLayout.tsx')))
}

// NF-FE-2 / NF-03: the box compares what is typed with the address AS IT WILL BE once the box's own commits have landed.
{
  // Address has q=abc. 'd' is typed and committed; Backspace comes before the router has echoed it.
  const c = fakeClock()
  const seen = []
  const box = SEARCH.createSearchController({ value: 'abc', delay: 300, scheduler: c.scheduler, onCommit: (q) => seen.push(q) })
  box.type('abcd')
  c.advance(300)
  assert.deepEqual(seen, ['abcd'])
  c.advance(20)
  box.type('abc')
  assert.equal(box.isPending(), true, 'NF-FE-2: "abc" is no longer what the address will say ("abcd"), so it is a change to commit')
  box.syncUrl('abcd')
  assert.equal(box.getText(), 'abc', 'the echo of the first commit does not overwrite the text')
  c.advance(300)
  assert.deepEqual(seen, ['abcd', 'abc'])
  box.syncUrl('abc')
  assert.equal(box.getText(), 'abc')
  assert.equal(box.isPending(), false)

  // NF-03: type 'x', pause, Backspace: the box ends empty AND the address ends empty.
  const c2 = fakeClock()
  const seen2 = []
  const box2 = SEARCH.createSearchController({ value: '', delay: 300, scheduler: c2.scheduler, onCommit: (q) => seen2.push(q) })
  box2.type('x')
  c2.advance(300)
  box2.type('')
  assert.equal(box2.isPending(), true, 'NF-03: the address is about to say "x": an empty box must commit the empty search')
  box2.syncUrl('x')
  assert.equal(box2.getText(), '')
  c2.advance(300)
  assert.deepEqual(seen2, ['x', ''])
  box2.syncUrl('')
  assert.equal(box2.getText(), '')

  // NF-03: the box's X button inside the same window.
  const c3 = fakeClock()
  const seen3 = []
  const box3 = SEARCH.createSearchController({ value: '', delay: 300, scheduler: c3.scheduler, onCommit: (q) => seen3.push(q) })
  box3.type('x')
  c3.advance(300)
  box3.clear()
  assert.deepEqual(seen3, ['x', ''], 'NF-03: clear() commits the empty search although the address still reads ""')
  box3.syncUrl('x')
  box3.syncUrl('')
  assert.equal(box3.getText(), '')

  // Two writes folded into one render: the first never echoes. The commit is forgotten after the echo window.
  const c4 = fakeClock()
  const box4 = SEARCH.createSearchController({ value: '', delay: 300, scheduler: c4.scheduler, onCommit() {} })
  box4.type('x')
  c4.advance(300)
  box4.type('')
  assert.equal(box4.isPending(), true, 'inside the window the address is still about to say "x"')
  box4.flush()
  c4.advance(SEARCH.ECHO_EXPIRY_MS)
  box4.type('')
  assert.equal(box4.isPending(), false, 'after the window the address is what it says')
  box4.syncUrl('x')
  assert.equal(box4.getText(), 'x', 'a later Back to that value is an outside change, not the echo of an old commit')
  box4.dispose()
  assert.equal(c4.pending(), 0, 'dispose leaves no timer behind')
}

// NF-05 / NF-06: a click that opens a new tab arms nothing; an armed focus move expires.
{
  const plain = { button: 0 }
  assert(CHIPFOCUS.isPlainClick(plain))
  for (const modified of [{ button: 0, ctrlKey: true }, { button: 0, metaKey: true }, { button: 0, shiftKey: true }, { button: 0, altKey: true }, { button: 1 }, { button: 2 }, { button: 0, defaultPrevented: true }]) {
    assert(!CHIPFOCUS.isPlainClick(modified), JSON.stringify(modified))
  }
  const links = ['a', 'b', 'c']
  assert.equal(CHIPFOCUS.chipFocusTarget(1, links, 'box'), 'b', 'the chip that took the removed one\'s place')
  assert.equal(CHIPFOCUS.chipFocusTarget(5, links, 'box'), 'c', 'the last chip when the removed one was last')
  assert.equal(CHIPFOCUS.chipFocusTarget(0, [], 'box'), 'box', 'the search box when no chip is left')
  assert.equal(CHIPFOCUS.chipFocusTarget(-1, links, 'box'), 'box', 'Clear all: the search box')
  assert(CHIPFOCUS.CHIP_FOCUS_WINDOW_MS >= 500 && CHIPFOCUS.CHIP_FOCUS_WINDOW_MS <= 5000)
  // The Changes "Clear all" link only empties the box for a plain click (NF-05).
  assert(/isPlainClick\(e\)/.test(read('src/components/inventory/ChangesToolbar.tsx')))
  // All three list toolbars share the chip list and the focus hook; Software gained it (UI-08 sibling, FV1-N1).
  for (const f of ['AssetToolbar', 'ChangesToolbar', 'SoftwareToolbar']) {
    const source = read(`src/components/inventory/${f}.tsx`)
    assert(/<FilterChipList/.test(source) && /useChipFocus\(/.test(source) && /focus\.removed\(index, e\)/.test(source), `${f}: chips go through FilterChipList and useChipFocus`)
  }
  assert(/focus\.cleared\(\)/.test(read('src/components/inventory/SoftwareToolbar.tsx')), 'the Software "Clear all" moves focus too')
  // The chip list itself: real links, remove markers, a contrast-safe prefix.
  const chips = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.FilterChipList, { items: [{ id: 'kind:server', label: 'Kind', text: 'Server', href: '/x' }, { id: 'tag:a', label: 'Tag', text: 'a', href: '/y' }] })))
  assert.equal((chips.match(/data-chip-remove/g) ?? []).length, 2)
  assert(chips.includes('aria-label="Remove filter Kind: Server"') && chips.includes('aria-label="Active filters"'))
  assert(!/class="[^"]*shrink-0 text-muted/.test(chips), 'NF-2: the chip prefix is not muted text on the pill fill')
}

// NF-07 / NF-FE-1: a share never rounds into a claim it cannot keep.
{
  assert.equal(M.fmtPct(99.95), '99.9%')
  assert.equal(M.fmtPct(99.96), '99.9%')
  assert.equal(M.fmtPct(100), '100.0%')
  assert.equal(M.fmtPct(0), '0.0%')
  assert.equal(M.fmtPct(0.04), '<0.1%')
  assert.equal(M.fmtPct(0.05), '0.1%')
  assert.equal(M.fmtPct(47.66), '47.7%')
  assert.equal(M.fmtPct(null), '—')
  assert.equal(M.fmtPctWords(99.96), '99.9 percent')
  assert.equal(M.fmtPctWords(0.04), '<0.1 percent')
  assert.equal(M.fmtShare(1999, 2000), '99.9%')
  assert.equal(M.fmtShare(1, 5000), '<0.1%')
  assert.equal(M.fmtShare(2000, 2000), '100.0%')
  assert.equal(M.fmtShare(0, 2000), '0.0%')
  assert.equal(M.fmtShare(0, 0), '—')
  // The Overview line: 1,999 of 2,000 with one without must not read 100.0%.
  const drill = (n) => ({ type: 'assets', filters: {}, count: n, href: '/inventory/assets' })
  const coverage = { covered: 1999, missing: 1, unknown: 0, answerable: 2000, total: 2000, pct: 99.95, coveredDrill: drill(1999), gapDrill: drill(1), unknownDrill: drill(0) }
  const line = decode(m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.AgentCoverageLine, { coverage }))).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
  assert(line.includes('(99.9%)') && !line.includes('100.0%') && line.includes('1 without'), `NF-07: ${line}`)
  assert(line.includes('of 2,000 servers with a complete software list'))
  const one = decode(m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.AgentCoverageLine, { coverage: { ...coverage, covered: 1, missing: 0, unknown: 0, answerable: 1, total: 1, pct: 100 } }))).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
  assert(one.includes('of 1 server with a complete software list') && one.includes('(100.0%)'), `a single covered server is exactly 100%: ${one}`)
  // No printed share uses toFixed(1) any more.
  const dir = 'src/components/inventory'
  for (const f of fs.readdirSync(path.join(root, dir)).filter((n) => /\.(ts|tsx)$/.test(n) && n !== 'model.ts')) {
    assert(!/pct[^\n]*\.toFixed\(1\)/.test(read(`${dir}/${f}`)), `${f}: shares go through fmtPct / fmtShare`)
  }
}

// NF-10 / NF-04: the banner announces the state, not the counts, and promises only what is true.
{
  const status = { normalising: true, pending_software_rows: 12000, pending_identity_rows: 1 }
  const html = m.renderToStaticMarkup(h(BANNER.DataStatusBanner, { status }))
  const live = html.match(/<div role="status"[^>]*data-data-status-live[^>]*>(.*?)<\/div>/)
  assert(live && live[1] === BANNER.NORMALISING_TEXT, 'the live region holds the static sentence only')
  assert(!/role="status"[^>]*>[^<]*\d/.test(html), 'no digit inside a live region: the counts change on every poll and would be announced again')
  const visible = decode(html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
  assert(visible.includes('12,000 software rows and 1 asset still to process at the last check'), visible)
  assert(!/updates by itself/.test(visible) && visible.includes('The figures refresh when processing finishes'), 'NF-04: says what is true')
  assert.equal(BANNER.backlogText({ pending_software_rows: 0, pending_identity_rows: 0 }), null)
  // Nothing to say: an empty live region only (it must exist before the banner to be announced).
  const quiet = m.renderToStaticMarkup(h(BANNER.DataStatusBanner, { status: { normalising: false, pending_software_rows: 0, pending_identity_rows: 0 } }))
  assert(!quiet.includes('data-data-status="normalising"') && quiet.includes('data-data-status-live'))
  assert.equal(m.renderToStaticMarkup(h(BANNER.DataStatusBanner, { status: null })).includes('data-data-status="normalising"'), false)
  // The refetch the wording relies on: when the backlog clears, every cached Inventory answer except settings and summary is refetched.
  assert.equal(META.normalisingFinished(true, false), true)
  assert.equal(META.normalisingFinished(true, true), false)
  assert.equal(META.normalisingFinished(false, false), false)
  assert.equal(META.normalisingFinished(undefined, false), false, 'a first answer is not a transition')
  assert.equal(META.normalisingFinished(true, undefined), false, 'a missing answer is not "finished"')
  assert(META.isInventoryData(['inventory', 'software', 'x']) && META.isInventoryData(['inventory', 'assets', 'q']) && META.isInventoryData(['inventory', 'os', 'server']))
  assert(!META.isInventoryData(['inventory', 'settings']) && !META.isInventoryData(['inventory', 'summary']) && !META.isInventoryData(['devices']))
  assert(/useRefreshWhenNormalised\(meta\.summary\?\.data_status\?\.normalising\)/.test(read('src/pages/inventory/InventoryLayout.tsx')), 'the layout wires it')
}

// NF-09: moving to another page mounts a new Suspense boundary, so the skeleton shows while its chunk downloads.
{
  assert.equal(m.pageBoundaryKey('/inventory/assets'), '/inventory/assets')
  assert.equal(m.pageBoundaryKey('/Inventory/Assets/'), '/inventory/assets')
  assert.notEqual(m.pageBoundaryKey('/inventory/assets'), m.pageBoundaryKey('/inventory/hardware'))
  assert.equal(m.pageBoundaryKey('/inventory/software/google-chrome'), m.pageBoundaryKey('/inventory/software/git'), 'two products are one page')
  assert.notEqual(m.pageBoundaryKey('/inventory/software'), m.pageBoundaryKey('/inventory/software/git'))
  assert(/<Suspense key=\{pageBoundaryKey\(pathname\)\}/.test(read('src/pages/inventory/InventoryLayout.tsx')), 'the boundary is keyed by page')
}

// NF-08: a number that cannot be opened keeps its name and gives its reason to a keyboard and a screen reader.
{
  const long = 'x'.repeat(250)
  const withLabel = render(h(m.DrillLink, { filters: { kind: 'server', cpu_model: long }, count: 7, 'aria-label': 'Xeon: 7 servers', 'data-drill': 'assets' }, '7'))
  assert(withLabel.includes('data-drill-unavailable') && withLabel.includes('role="img"') && withLabel.includes('aria-label="Xeon: 7 servers"'), withLabel)
  assert(withLabel.includes('data-drill="assets"') && withLabel.includes('data-drill-count="7"'), 'other data markers are kept')
  const describedBy = withLabel.match(/aria-describedby="([^"]+)"/)
  assert(describedBy && new RegExp(`<span id="${describedBy[1].replace(/[:]/g, '\\$&')}" hidden="">No list can be opened`).test(withLabel), 'NF-08: the reason is text a screen reader reaches, not only a tooltip')
  const bare = render(h(m.DrillLink, { filters: { cpu_model: long }, count: 3 }, '3'))
  assert(bare.includes('data-drill-unavailable') && /<span class="sr-only"> \(No list can be opened/.test(bare), 'without a label the reason follows the number as text')
  const fine = render(h(m.DrillLink, { filters: { kind: 'server' }, count: 3, 'aria-label': 'Servers: 3' }, '3'))
  assert(fine.includes('<a ') && fine.includes('aria-label="Servers: 3"') && !fine.includes('unavailable'))
}

// NF-FE-6 / NB-05: the client trims the way the API does and refuses a value that could not round-trip.
{
  // What str.strip() removes (checked against CPython once; pinned here).
  const stripped = ['\t', '\n', '\u000b', '\f', '\r', '\u001c', '\u001d', '\u001e', '\u001f', ' ', '\u0085', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', '　']
  const kept = ['﻿', '​', '᠎', '⁠', 'x']
  for (const c of stripped) assert.equal(F.apiTrim(`${c}Annex${c}`), 'Annex', `U+${c.charCodeAt(0).toString(16)} is trimmed like the API does`)
  for (const c of kept) assert.equal(F.apiTrim(`${c}Annex${c}`), `${c}Annex${c}`, `U+${c.charCodeAt(0).toString(16)} is not trimmed by the API`)
  assert.equal(F.apiTrim('DR  /  rack 2'), 'DR  /  rack 2', 'inner runs of spaces stay')
  // Reading an address by hand is lenient: the API trims the same way, so the same list opens.
  assert.deepEqual(F.parseAssetFilters('?site=Annex%C2%A0').filters, { site: 'Annex' })
  assert.deepEqual(F.parseAssetFilters('?site=%20Annex%20').filters, { site: 'Annex' })
  assert.deepEqual(F.parseAssetFilters('?site=Annex%0B').filters, { site: 'Annex' })
  assert.deepEqual(F.parseAssetFilters('?site=%C2%85Annex').filters, { site: 'Annex' }, 'U+0085 is trimmed by the API (and not by JavaScript)')
  assert.deepEqual(F.parseAssetFilters('?site=%EF%BB%BFAnnex').filters, { site: '\ufeffAnnex' }, 'U+FEFF is trimmed by JavaScript (and not by the API): it stays part of the value')
  // A value that comes from the API must come back unchanged: edge white space other than plain spaces cannot.
  for (const v of ['Annex ', '\tAnnex', 'Annex　', 'Annex\n']) {
    const { rejected, filters } = F.normaliseAssetFilters({ site: v })
    assert.equal(rejected.length, 1, JSON.stringify(v))
    assert.equal(filters.site, undefined, 'the value is never changed into another one')
    assert(/white space the API would drop/.test(rejected[0].reason))
  }
  assert.deepEqual(F.normaliseAssetFilters({ site: ' Annex ' }).filters, { site: 'Annex' }, 'plain spaces at the ends are what SQL btrim removes too')
  assert.deepEqual(F.normaliseAssetFilters({ site: 'Annex Rack' }).filters, { site: 'Annex Rack' }, 'NBSP inside a name round-trips')
  assert.equal(F.normaliseAssetFilters({ tag: ['a '] }).rejected.length, 1)
  // The number is then plain text with the reason, and so is the facet row.
  const nbsp = render(h(m.DrillLink, { filters: { site: 'Annex ' }, count: 1, 'aria-label': 'Annex: 1 asset' }, '1'))
  assert(nbsp.includes('data-drill-unavailable') && nbsp.includes('white space the API would drop') && !nbsp.includes('<a '))
  const group = { key: 'site', label: 'Site', items: [{ value: 'Annex ', count: 1 }, { value: 'Annex', count: 4 }, { value: 'Tab\there', count: 2 }] }
  assert(FP.facetItemUnavailable(group, group.items[0]).includes('No list can be opened'))
  assert.equal(FP.facetItemUnavailable(group, group.items[1]), null)
  assert(FP.facetItemUnavailable(group, group.items[2]).includes('control characters'), 'a tab inside the value cannot be written either: shown as text, not as a link that applies no filter')
  const panel = render(h(m.FacetPanel, { groups: [group], filters: {}, hrefFor: (patch) => `/inventory/assets?${new URLSearchParams(patch.site ? { site: patch.site } : {})}` }))
  assert.equal((panel.match(/data-drill-unavailable/g) ?? []).length, 2, 'two rows are text')
  assert.equal(anchors(panel).length, 1, 'one row is a link')
  assert(anchors(panel)[0].href.endsWith('site=Annex'))
}

// NF-FE-4: a trailing slash does not skip the gate of an end:true page.
{
  const params = new URLSearchParams()
  for (const p of ['/reports', '/alerts', '/subscription', '/inventory', '/devices']) {
    const a = NAV.trailForLocation(p, params)
    for (const variant of [`${p}/`, `${p}//`, p.toUpperCase()]) {
      const b = NAV.trailForLocation(variant, params)
      assert.deepEqual(b && [b.group.id, b.item.to, b.child?.to], a && [a.group.id, a.item.to, a.child?.to], `${variant} is the same page as ${p}`)
    }
  }
  assert(NAV.trailForLocation('/reports/', params), 'NF-FE-4: /reports/ is a known page')
}

// X9: the endpoint-protection reasons, verbatim, and a generic sentence for a reason this build does not know.
{
  assert.equal(AM.securityAgentReasonText('no_software_list'), 'No software list has been reported for this server')
  assert.equal(AM.securityAgentReasonText('list_truncated'), 'The software list was cut off, so protection cannot be judged')
  assert.equal(AM.securityAgentReasonText('normalising'), 'The software list is still being processed')
  assert.equal(AM.securityAgentReasonText('no_service_list'), 'Built-in Windows Defender can only be seen through the service list, and this server has not reported one')
  assert.equal(AM.securityAgentReasonText('something_new'), AM.SECURITY_AGENT_REASON_FALLBACK)
  assert.equal(AM.securityAgentReasonText('constructor'), AM.SECURITY_AGENT_REASON_FALLBACK, 'a reason named like an object member is still just unknown')
  assert.equal(AM.securityAgentReasonText(null), null)
  assert.equal(AM.securityAgentReasonText(undefined), null)
  assert.equal(AM.securityAgentSummary({ present: null, state: 'unknown', reason: 'no_service_list', products: [] }).detail, AM.SECURITY_AGENT_REASON.no_service_list)
  assert.equal(AM.securityAgentSummary({ present: null, state: 'unknown', reason: 'future_reason', products: [] }).detail, AM.SECURITY_AGENT_REASON_FALLBACK)
  // None of the sentences or the Overview line promise that the server will report by itself (FV1-N6).
  for (const text of [...Object.values(AM.SECURITY_AGENT_REASON), AM.SECURITY_AGENT_REASON_FALLBACK, OV.unknownServersText(2), OV.UNKNOWN_SERVERS_HINT]) {
    assert(!/\b(yet|soon|shortly|will)\b/i.test(text.replace('still being processed', '')), `no transient promise: ${text}`)
  }
  assert(OV.UNKNOWN_SERVERS_HINT.includes('Linux') && OV.UNKNOWN_SERVERS_HINT.includes('service list'), 'the hint names the servers that cannot report and the Defender case')
  const flyoutHtml = (reason) => decode(m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.FlyoutBody, { detail: (() => { const d = structuredClone(JSON.parse(fs.readFileSync(path.join(root, 'src/pages/inventory/fixtures/asset-server.json'), 'utf8'))); d.server.software.security_agent = { present: null, state: 'unknown', reason, products: [], services: [] }; return d })() }))).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '))
  setUser(USER(['inventory.view', 'servers.view', 'devices.view', 'discovery.view']))
  assert(flyoutHtml('no_service_list').includes('Built-in Windows Defender can only be seen through the service list'))
  assert(flyoutHtml('brand_new').includes(AM.SECURITY_AGENT_REASON_FALLBACK))
}


// FE-02 / FE-07 / NF-02 / NF-03 / UI-08 through the REAL hooks (review findings NF-FE-3, FV1-N5). There is no DOM here, so the hooks
// run on a minimal hook dispatcher (the same one React installs for a render: state, refs, memo, effects and external stores
// are all real hook calls) with a fake router that echoes a write after a delay, like the real one does after a render.
{
  const harnessSource = `
    import * as React from 'react'
    const internals = React.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
    const same = (a, b) => !!a && !!b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
    export function renderHook(callback) {
      const slots = []
      const registry = new Set()
      let cursor = 0
      let dirty = false
      let result
      let renders = 0
      let effects = []
      const effect = (fn, deps) => {
        const i = cursor++
        const slot = (slots[i] ??= { deps: undefined, cleanup: undefined, ran: false })
        if (slot.ran && deps && same(slot.deps, deps)) return
        effects.push({ slot, fn, deps })
      }
      const dispatcher = {
        useState(initial) {
          const slot = (slots[cursor] ??= { value: typeof initial === 'function' ? initial() : initial })
          cursor++
          slot.set ??= (next) => {
            const value = typeof next === 'function' ? next(slot.value) : next
            if (!Object.is(value, slot.value)) { slot.value = value; dirty = true }
          }
          return [slot.value, slot.set]
        },
        useRef(initial) { const slot = (slots[cursor] ??= { current: initial }); cursor++; return slot },
        useMemo(factory, deps) {
          const i = cursor++
          const slot = slots[i]
          if (slot && deps && same(slot.deps, deps)) return slot.value
          const value = factory()
          slots[i] = { value, deps }
          return value
        },
        useCallback(fn, deps) { return dispatcher.useMemo(() => fn, deps) },
        useEffect: effect,
        useLayoutEffect: effect,
        useSyncExternalStore(subscribe, getSnapshot) {
          const slot = (slots[cursor] ??= {})
          cursor++
          const value = getSnapshot()
          if (!slot.subscribed) {
            slot.subscribed = true
            effects.push({ slot: {}, fn: () => subscribe(() => { if (!Object.is(getSnapshot(), slot.last)) dirty = true }), deps: undefined })
          }
          slot.last = value
          return value
        },
        useDebugValue() {},
        useId() { const slot = (slots[cursor] ??= { value: ':h' + cursor + ':' }); cursor++; return slot.value },
      }
      function renderOnce() {
        const previous = internals.ReactCurrentDispatcher.current
        internals.ReactCurrentDispatcher.current = dispatcher
        cursor = 0
        effects = []
        try { result = callback() } finally { internals.ReactCurrentDispatcher.current = previous }
        renders++
        const run = effects
        effects = []
        for (const e of run) {
          e.slot.cleanup?.()
          const cleanup = e.fn()
          e.slot.cleanup = typeof cleanup === 'function' ? cleanup : undefined
          e.slot.deps = e.deps
          e.slot.ran = true
          registry.add(e.slot)
        }
      }
      function flush() {
        for (let n = 0; dirty; n++) {
          if (n > 50) throw new Error('too many re-renders')
          dirty = false
          renderOnce()
        }
      }
      renderOnce()
      flush()
      return {
        get result() { return result },
        get renders() { return renders },
        act(fn) { fn(); flush() },
        rerender() { dirty = true; flush() },
        unmount() { for (const s of registry) s.cleanup?.(); registry.clear() },
      }
    }
  `
  const fakeRouterSource = `
    import { useCallback, useMemo, useSyncExternalStore } from 'react'
    export const router = { pathname: '/inventory/assets', search: '', state: null, listeners: new Set(), writes: [], navigations: [], echoMs: 0, later: (fn) => fn() }
    const subscribe = (l) => { router.listeners.add(l); return () => router.listeners.delete(l) }
    const notify = () => { for (const l of [...router.listeners]) l() }
    export function reset(search = '', pathname = '/inventory/assets') {
      Object.assign(router, { pathname, search, state: null, writes: [], navigations: [], echoMs: 0 })
    }
    export function useSearchParams() {
      const search = useSyncExternalStore(subscribe, () => router.search)
      const params = useMemo(() => new URLSearchParams(search), [search])
      const set = useCallback((next, options = {}) => {
        // Like the real hook: the functional form receives the params of the RENDER that created this function.
        const value = new URLSearchParams(typeof next === 'function' ? next(params) : next).toString()
        router.writes.push({ search: value, replace: !!options.replace })
        const land = () => { router.search = value; router.state = options.state ?? null; notify() }
        if (router.echoMs > 0) router.later(land, router.echoMs); else land()
      }, [params])
      return [params, set]
    }
    export function useLocation() {
      const search = useSyncExternalStore(subscribe, () => router.search)
      return { pathname: router.pathname, search: search ? '?' + search : '', state: router.state }
    }
    export function useNavigate() { return useCallback((to) => { router.navigations.push(to) }, []) }
  `
  const hooksEntry = `
    export { useInventoryUrlState } from '@/pages/inventory/useInventoryUrlState'
    export { useSoftwareUrlState } from '@/components/inventory/useSoftwareUrlState'
    export { useDebouncedSearch } from '@/components/inventory/useDebouncedSearch'
    export { useChipFocus } from '@/components/inventory/useChipFocus'
    export { renderHook } from 'virtual:harness'
    export * as fake from 'virtual:fake-router'
  `
  const virtual = { 'virtual:harness': harnessSource, 'virtual:fake-router': fakeRouterSource }
  const hooksBundle = await build({
    stdin: { contents: hooksEntry, resolveDir: root, loader: 'ts' },
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'browser',
    target: 'es2022',
    jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' },
    plugins: [
      aliasPlugin,
      {
        name: 'virtual-modules',
        setup(b) {
          b.onResolve({ filter: /^react-router-dom$/ }, () => ({ path: 'virtual:fake-router', namespace: 'virtual' }))
          b.onResolve({ filter: /^virtual:/ }, (args) => ({ path: args.path, namespace: 'virtual' }))
          b.onLoad({ filter: /.*/, namespace: 'virtual' }, (args) => ({ contents: virtual[args.path], loader: 'js', resolveDir: root }))
        },
      },
    ],
    logLevel: 'silent',
  })
  const hk = await import(`data:text/javascript;base64,${Buffer.from(hooksBundle.outputFiles[0].text).toString('base64')}`)
  const router = hk.fake.router

  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  const realWindow = globalThis.window
  const clock = fakeClock()
  globalThis.setTimeout = clock.scheduler.setTimeout
  globalThis.clearTimeout = clock.scheduler.clearTimeout
  router.later = (fn, ms) => clock.scheduler.setTimeout(fn, ms)
  const filtersOf = () => F.parseAssetFilters(router.search).filters
  try {
    // FE-02 + FE-07: typing, then a facet clicked inside the debounce; both survive and the box is not rewound.
    {
      hk.fake.reset('')
      router.echoMs = 15
      let renderNo = 0
      const seenRenders = []
      const page = hk.renderHook(() => {
        const mine = ++renderNo
        const url = hk.useInventoryUrlState()
        const search = hk.useDebouncedSearch({ value: url.filters.q ?? '', onCommit: (q) => { seenRenders.push(mine); url.patchFilters({ q }) } })
        return { url, search }
      })
      page.act(() => page.result.search.onChange('srv'))
      page.act(() => clock.advance(100))
      page.act(() => page.result.url.patchFilters({ kind: 'server' }))
      page.act(() => clock.advance(15))
      assert.deepEqual(filtersOf(), { kind: 'server' }, 'the facet landed first')
      page.act(() => clock.advance(200))
      page.act(() => clock.advance(50))
      assert.deepEqual(filtersOf(), { q: 'srv', kind: 'server' }, 'FE-07: the search landing later does not undo the facet clicked while it waited')
      assert.equal(page.result.search.text, 'srv')
      assert.equal(router.writes.at(-1).replace, true, 'typing replaces the history entry')
      assert.equal(router.writes[0].replace, false, 'a facet pushes')
      // The commit callback is the one of the LATEST render, not of the render that typed (setOnCommit): its closure saw the newest render number.
      assert.equal(seenRenders.length, 1)
      assert(seenRenders[0] >= 3, `FE-02: the commit used the latest callback (render ${seenRenders[0]})`)
      // A character typed 20 ms after the commit fired survives the echo.
      page.act(() => page.result.search.onChange('srvx'))
      page.act(() => clock.advance(300))
      page.act(() => page.result.search.onChange('srvxy'))
      page.act(() => clock.advance(15))
      assert.equal(page.result.search.text, 'srvxy', 'FE-02: the echo of the box\'s own commit does not rewind the box')
      page.act(() => clock.advance(400))
      assert.equal(filtersOf().q, 'srvxy')
      assert.equal(page.result.search.text, 'srvxy')
      page.unmount()
    }

    // FE-07: two changes made before the router has re-rendered compose; the second never undoes the first.
    {
      for (const kind of ['assets', 'software']) {
        hk.fake.reset('')
        router.echoMs = 15
        globalThis.window = { location: { pathname: router.pathname } }
        const page = hk.renderHook(() => (kind === 'assets' ? hk.useInventoryUrlState() : hk.useSoftwareUrlState()))
        page.act(() => {
          if (kind === 'assets') { page.result.patchFilters({ kind: 'server' }); page.result.patchFilters({ vendor: 'Cisco' }) }
          else { page.result.patchFilters({ category: ['browser'] }); page.result.patchFilters({ publisher: 'Google' }) }
        })
        page.act(() => clock.advance(100))
        const p = new URLSearchParams(router.search)
        if (kind === 'assets') assert.deepEqual([p.get('kind'), p.get('vendor')], ['server', 'Cisco'], 'FE-07: two clicks in one tick both survive')
        else assert.deepEqual([p.get('category'), p.get('publisher')], ['browser', 'Google'], 'FE-07 (software): two changes in one tick both survive')
        page.unmount()
      }
    }

    // NF-FE-2 / NF-03 in the hooks, with the router echoing 70 ms after a write.
    {
      hk.fake.reset('q=abc')
      router.echoMs = 70
      const page = hk.renderHook(() => {
        const url = hk.useInventoryUrlState()
        const search = hk.useDebouncedSearch({ value: url.filters.q ?? '', onCommit: (q) => url.patchFilters({ q }) })
        return { url, search }
      })
      assert.equal(page.result.search.text, 'abc')
      page.act(() => page.result.search.onChange('abcd'))
      page.act(() => clock.advance(300))
      page.act(() => clock.advance(20))
      page.act(() => page.result.search.onChange('abc'))
      page.act(() => clock.advance(1000))
      assert.equal(page.result.search.text, 'abc', 'NF-FE-2: the box shows what was typed last')
      assert.equal(filtersOf().q, 'abc', 'NF-FE-2: and the address agrees')
      page.act(() => page.result.search.onChange('x'))
      page.act(() => clock.advance(300))
      page.act(() => clock.advance(20))
      page.act(() => page.result.search.onChange(''))
      page.act(() => clock.advance(1000))
      assert.equal(page.result.search.text, '')
      assert.equal(filtersOf().q, undefined, 'NF-03: box and address end empty together')
      page.act(() => page.result.search.onChange('y'))
      page.act(() => clock.advance(300))
      page.act(() => clock.advance(20))
      page.act(() => page.result.search.clear())
      page.act(() => clock.advance(1000))
      assert.equal(page.result.search.text, '')
      assert.equal(filtersOf().q, undefined, 'NF-03: the X button in the same window')
      page.unmount()
    }

    // NF-02: a search that lands after the user clicked another tab (whose chunk is still downloading) must not navigate back.
    {
      for (const useHook of ['assets', 'software']) {
        hk.fake.reset('', useHook === 'assets' ? '/inventory/assets' : '/inventory/software')
        router.echoMs = 0
        globalThis.window = { location: { pathname: router.pathname } }
        const page = hk.renderHook(() => {
          if (useHook === 'assets') {
            const url = hk.useInventoryUrlState()
            const search = hk.useDebouncedSearch({ value: url.filters.q ?? '', onCommit: (q) => url.patchFilters({ q }) })
            return { search }
          }
          const url = hk.useSoftwareUrlState()
          const search = hk.useDebouncedSearch({ value: url.filters.q ?? '', onCommit: (q) => url.patchFilters({ q: q || undefined }) })
          return { search }
        })
        // Control: on the page, the search is written.
        page.act(() => page.result.search.onChange('win'))
        page.act(() => clock.advance(300))
        assert.equal(router.writes.length, 1, `${useHook}: on its own page the search is written`)
        // The user types, then clicks another tab 100 ms later; the address is the new page's, the old page is still mounted.
        page.act(() => page.result.search.onChange('winx'))
        page.act(() => clock.advance(100))
        globalThis.window = { location: { pathname: useHook === 'assets' ? '/inventory/hardware' : '/inventory/changes' } }
        page.act(() => clock.advance(300))
        assert.equal(router.writes.length, 1, `NF-02 (${useHook}): the old page writes nothing once the address has moved on`)
        page.unmount()
      }
      // The same guard, as the pure rule.
      assert(U.addressIsPage('/inventory/assets', { location: { pathname: '/inventory/assets' } }))
      assert(U.addressIsPage('/inventory/assets/', { location: { pathname: '/Inventory/Assets' } }), 'case and a trailing slash do not matter')
      assert(U.addressIsPage('/inventory/software/a%20b', { location: { pathname: '/inventory/software/a b' } }), 'encoding does not matter')
      assert(!U.addressIsPage('/inventory/assets', { location: { pathname: '/inventory/hardware' } }))
      const inPlace = globalThis.window
      delete globalThis.window
      assert(U.addressIsPage('/inventory/assets'), 'no window: always')
      globalThis.window = inPlace
    }

    // FE-07 for the Software hook: a category clicked while the text waited survives it.
    {
      hk.fake.reset('')
      router.echoMs = 15
      globalThis.window = { location: { pathname: router.pathname } }
      const page = hk.renderHook(() => {
        const url = hk.useSoftwareUrlState()
        const search = hk.useDebouncedSearch({ value: url.filters.q ?? '', onCommit: (q) => url.patchFilters({ q: q || undefined }) })
        return { url, search }
      })
      page.act(() => page.result.search.onChange('chrome'))
      page.act(() => clock.advance(100))
      page.act(() => page.result.url.patchFilters({ category: ['browser'] }))
      page.act(() => clock.advance(15))
      page.act(() => clock.advance(300))
      page.act(() => clock.advance(30))
      const p = new URLSearchParams(router.search)
      assert.equal(p.get('q'), 'chrome', 'FE-07 (software): the text landed')
      assert.equal(p.get('category'), 'browser', 'FE-07 (software): and so did the category')
      page.unmount()
    }

    // UI-08: the focus really moves (a spy on focus()), and only for a plain click.
    {
      const link = (name) => ({ name, focused: 0, focus() { this.focused++ } })
      let chips = [link('a'), link('b'), link('c')]
      const list = { current: { querySelectorAll: () => chips } }
      const box = link('box')
      const fallback = { current: box }
      const props = { signature: 'a|b|c' }
      const page = hk.renderHook(() => hk.useChipFocus(list, fallback, props.signature))
      // A removed middle chip: focus goes to the chip that took its place.
      page.act(() => page.result.removed(1, { button: 0 }))
      chips = [chips[0], chips[2]]
      props.signature = 'a|c'
      page.rerender()
      assert.equal(chips[1].focused, 1, 'UI-08: focus moves to the chip that took the removed one\'s place')
      // The last chip removed: the one before it.
      page.act(() => page.result.removed(1, { button: 0 }))
      chips = [chips[0]]
      props.signature = 'a'
      page.rerender()
      assert.equal(chips[0].focused, 1)
      // Nothing left: the search box. Clear all: the search box.
      page.act(() => page.result.removed(0, { button: 0 }))
      chips = []
      props.signature = ''
      page.rerender()
      assert.equal(box.focused, 1, 'UI-08: no chip left, focus goes to the search box')
      chips = [link('p'), link('q')]
      props.signature = 'p|q'
      page.rerender()
      page.act(() => page.result.cleared({ button: 0 }))
      props.signature = 'p|q|r'
      page.rerender()
      assert.equal(box.focused, 2, 'Clear all: the search box')
      // NF-06: a Ctrl-click on a chip opens a new tab; the next, unrelated change must not steal focus.
      page.act(() => page.result.removed(0, { button: 0, ctrlKey: true }))
      props.signature = 'p|q|r|s'
      page.rerender()
      assert.equal(box.focused, 2, 'NF-06: a modified click armed nothing')
      assert.equal(chips[0].focused, 0)
      // An armed move that never sees the list change expires instead of firing on a later, unrelated change.
      page.act(() => page.result.removed(0, { button: 0 }))
      page.act(() => clock.advance(CHIPFOCUS.CHIP_FOCUS_WINDOW_MS + 10))
      props.signature = 'p|q|r|s|t'
      page.rerender()
      assert.equal(chips[0].focused, 0, 'NF-06: an expired move does not fire')
      page.unmount()
    }
    assert.equal(clock.pending(), 0, 'no timer is left behind by the hooks after unmount')
  } finally {
    globalThis.setTimeout = realSetTimeout
    globalThis.clearTimeout = realClearTimeout
    if (realWindow === undefined) delete globalThis.window
    else globalThis.window = realWindow
  }
}


console.log('Inventory UI: every navigation target resolves to a declared route; the filter vocabulary round-trips and rejects what it does not know; DrillLink, badges, bars, facets, meta-line, access gate and layout render as specified; URL state is read and written by the rules; types.ts matches the golden API fixtures; the Overview and Assets pages map every number to a vocabulary-only drill link, render from the golden responses, and the flyout follows the permissions of the pages it links to.')
