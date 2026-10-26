// Inventory Software catalog, product page and settings (Documentation/Inventory/04-ROADMAP task 1.7 and the
// minimal Settings page):
//   (a) the response types against the backend's golden fixtures: vocabularies, cross-file equalities,
//       and the compile-time conformance file still carries its negative controls,
//   (b) the catalog URL vocabulary and state (filters, sort rules, paging, the hide toggle),
//   (c) the catalog's version bar, footer and drill links (principle 1: a number's list has that total),
//   (d) the product page model: tabs and their URL state, version ranking labels, KPIs, the installation list,
//       the timeline (pairing of an installation and a removal), the evidence,
//   (e) the settings model: draft, inline validation, the PUT diff, the toast words, the hidden categories,
//   (f) the components rendered to markup: the catalog, the product page tabs, the settings form in every state.
// Run from dashboard/: node scripts/test-inventory-software-settings.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
const fixture = (name) => JSON.parse(read(`src/pages/inventory/fixtures/${name}.json`))

/* ── bundle the code under test (the app's own alias, real components) ─────────────────────── */

const entry = `
export * as sm from '@/components/inventory/softwareModel'
export * as pm from '@/components/inventory/productModel'
export * as st from '@/components/inventory/settingsModel'
export * as sw from '@/pages/inventory/softwareSettings'
export * as F from '@/pages/inventory/filters'
export * as cm from '@/components/inventory/changeModel'
export * as chm from '@/components/inventory/changesModel'
export * as usu from '@/components/inventory/useSoftwareUrlState'
export * as hs from '@/components/inventory/useSoftwareHistoryStart'
export * as sbm from '@/components/inventory/searchBoxModel'
export * as bd from '@/components/layout/breadcrumbDetail'
export * as dw from '@/components/layout/deniedWording'
export * as NAV from '@/components/layout/navigation'
export { SoftwareTable } from '@/components/inventory/SoftwareTable'
export { SoftwareToolbar } from '@/components/inventory/SoftwareToolbar'
export { SoftwareFooter } from '@/components/inventory/SoftwareFooter'
export { SoftwareVersionBar } from '@/components/inventory/SoftwareVersionBar'
export { ProductHeader } from '@/components/inventory/ProductHeader'
export { ProductVersionsTab } from '@/components/inventory/ProductVersionsTab'
export { ProductEvidenceTab } from '@/components/inventory/ProductEvidenceTab'
export { SettingsEditor } from '@/components/inventory/SettingsEditor'
export { InventorySoftwarePage } from '@/pages/inventory/InventorySoftwarePage'
export { InventoryProductPage } from '@/pages/inventory/InventoryProductPage'
export { InventorySettingsPage } from '@/pages/inventory/InventorySettingsPage'
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
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { sm: S, pm: P, st: T, sw: API, F, cm: CM, chm: CH, usu: USU, hs: HS, sbm: SBM, bd: BD, dw: DW, NAV } = m
const sp = (q) => new URLSearchParams(q)

const CATALOG = fixture('software')
const CATALOG_SCOPED = fixture('software-scoped')
const PUBLISHERS = fixture('software-publishers')
const PRODUCT = fixture('software-product')
const PRODUCT_SCOPED = fixture('software-product-scoped')
const INSTALLS = fixture('software-installations')
const SETTINGS = fixture('settings')

/* ═══ (a) the types against the golden fixtures ═════════════════════════════════════════════ */

{
  // The compile-time check (softwareSettings.typecheck.ts) is only worth anything while its negative controls
  // still exist: each @ts-expect-error must keep erroring, and the build fails when one starts to compile.
  const tc = read('src/pages/inventory/softwareSettings.typecheck.ts')
  assert.equal((tc.match(/^\/\/ @ts-expect-error/gm) ?? []).length, 5, 'five negative controls')
  for (const f of ['software', 'software-scoped', 'software-publishers', 'software-product', 'software-product-scoped', 'software-installations', 'software-installations-scoped', 'settings']) {
    assert(tc.includes(`./fixtures/${f}.json`), `${f} is checked`)
  }
  assert(tc.includes('NoExtraSettings') && tc.includes('NoExtraProduct') && tc.includes('NoExtraSoftware'), 'both directions')
}

// The closed vocabularies at run time (the compile-time check can only say "string").
{
  const STATUS = new Set(['normalized', 'publisher_only', 'unmatched'])
  const SCHEMES = new Set(['semverish', 'dpkg', 'rpm'])
  const GENERATIONS = new Set(['N', 'N-1', 'older', null])
  for (const c of [CATALOG, CATALOG_SCOPED]) {
    for (const row of c.items) {
      assert(STATUS.has(row.norm_status), row.product_key)
      assert(S.SOFTWARE_CATEGORIES.includes(row.category), `${row.product_key}: category ${row.category} is in the vocabulary`)
      for (const v of row.distribution.top) assert(SCHEMES.has(v.scheme))
      assert(row.distribution.top.length <= 8)
      assert(row.hosts <= row.installs, 'hosts never exceed installs')
    }
    assert(c.hidden_categories.every((x) => S.SOFTWARE_CATEGORIES.includes(x)))
    assert(S.CATALOG_SORT_KEYS.includes(c.sort), 'the echoed sort is one the page can send')
  }
  for (const p of [PRODUCT, PRODUCT_SCOPED]) {
    assert(STATUS.has(p.header.norm_status))
    for (const v of p.versions) { assert(SCHEMES.has(v.scheme)); assert(GENERATIONS.has(v.generation)) }
  }
  assert.deepEqual([...SETTINGS.categories], [...S.SOFTWARE_CATEGORIES], 'the page vocabulary of categories is the settings vocabulary')
  assert.deepEqual(Object.keys(SETTINGS.limits).sort(), ['change_retention_days', 'fresh_hours', 'snapshot_hour', 'stale_days'])
  for (const [k, l] of Object.entries(SETTINGS.limits)) assert(l.min < l.max, k)
  assert(INSTALLS.items.every((i) => i.asset_key === `server:${i.asset_id}`), 'an installation addresses its server as server:<id>')
  assert(S.CATALOG_SORT_KEYS.includes('hosts') && P.INSTALL_SORT_KEYS.includes(INSTALLS.sort))
}

// Cross-file equalities the page relies on (principle 1): the catalog row, the product header and the version rows say the same numbers.
{
  const git = CATALOG.items.find((r) => r.product_key === 'git')
  assert.equal(PRODUCT.header.hosts, git.hosts)
  assert.equal(PRODUCT.header.installs, git.installs)
  assert.equal(PRODUCT.header.version_count, git.version_count)
  assert.equal(PRODUCT.header.latest_version, git.latest_version)
  assert.equal(PRODUCT.versions.length, PRODUCT.header.version_count, 'the Versions tab has as many rows as the version count')
  assert.equal(INSTALLS.total, PRODUCT.header.installs, 'the installation list has as many rows as the installs number')
  // The bar's per-version hosts are the product page's.
  for (const v of git.distribution.top) {
    const row = PRODUCT.versions.find((x) => x.version === v.version && x.scheme === v.scheme)
    assert.equal(row.hosts, v.hosts, v.version)
    assert.equal(row.rank, v.rank, v.version)
  }
  assert.equal(PRODUCT.versions.reduce((s, v) => s + v.installs, 0) + PRODUCT.unversioned.installs, PRODUCT.header.installs, 'every installation is in a version or in unversioned')
  assert.equal(CATALOG.total, CATALOG.items.length, 'the golden set holds the whole default catalog')
  assert.equal(PUBLISHERS.total, CATALOG.total, 'the publisher facet and the catalog count the same products')
  assert.equal(PUBLISHERS.items.reduce((s, p) => s + p.products, 0) <= PUBLISHERS.total, true)
}

/* ═══ (b) the catalog URL vocabulary and state ══════════════════════════════════════════════ */

assert.deepEqual([...S.CATALOG_FILTER_KEYS], ['q', 'category', 'publisher', 'hidden', 'unmatched'], 'the page URL names the API parameters')
assert.deepEqual([...S.CATALOG_SORT_KEYS], ['name', 'publisher', 'category', 'hosts', 'version_count', 'latest_version', 'first_seen'], 'the sorts of GET /software')

{
  const full = { q: 'visual c++', category: ['runtime', 'browser'], publisher: 'microsoft', hidden: 'include', unmatched: true }
  const qs = S.writeCatalogFilters(full).toString()
  assert.equal(qs, 'q=visual+c%2B%2B&category=browser&category=runtime&publisher=microsoft&hidden=include&unmatched=true', 'categories are a set, written in a canonical order')
  const back = S.parseCatalogFilters(qs)
  assert.deepEqual(back.filters, { ...full, category: ['browser', 'runtime'] })
  assert.deepEqual(back.rejected, [])
  assert.equal(S.writeCatalogFilters(back.filters).toString(), qs, 'writing again gives the same string')
  assert.deepEqual(S.parseCatalogFilters('').filters, {})
  assert.deepEqual(S.parseCatalogFilters(`?${qs}`).filters, back.filters, 'a leading question mark is fine')
  // The defaults are not written: a plain catalog has a plain URL.
  assert.equal(S.writeCatalogFilters({ hidden: undefined }).toString(), '')
  assert.deepEqual(S.parseCatalogFilters('hidden=exclude').filters, {})
  assert.deepEqual(S.parseCatalogFilters('unmatched=false').filters, {})
  assert.deepEqual(S.parseCatalogFilters('unmatched=1').filters, { unmatched: true })
}
{
  // Bad values are reported, never passed on (the API answers 422 for them).
  const bad = S.parseCatalogFilters('category=boots&category=BROWSER&hidden=maybe&unmatched=sometimes&publisher=%20&q=' + 'x'.repeat(201))
  assert.deepEqual(bad.filters, { category: ['browser'] }, 'categories are case-insensitive; blank text is no filter')
  assert.deepEqual(bad.rejected.map((r) => r.key).sort(), ['category', 'hidden', 'q', 'unmatched'])
  for (const r of bad.rejected) assert(r.reason)
  assert.equal(S.writeCatalogFilters({ category: ['nope'] }).toString(), '', 'writing validates like reading')
  assert.equal(S.categoryLabel('remote-access'), 'Remote access')
  assert.equal(S.categoryLabel('os-component'), 'OS component')
  assert.equal(S.categoryLabel('browser'), 'Browser')
  assert.equal(S.categoryLabel(''), 'Other')
  assert.equal(S.categoryLabel('weird_thing'), 'Weird thing', 'an unknown category is shown tidied, not dropped')
}
{
  // Chips: one per category, the publisher and the unmatched filter; not the search text and not the toggle.
  const chips = S.catalogFilterChips({ q: 'x', category: ['browser', 'remote-access'], publisher: 'microsoft', hidden: 'include', unmatched: true }, (v) => (v === 'microsoft' ? 'Microsoft' : undefined))
  assert.deepEqual(chips.map((c) => `${c.label}: ${c.text}`), ['Category: Browser', 'Category: Remote access', 'Publisher: Microsoft', 'Status: Unmatched only'])
  assert.equal(S.catalogFilterChips({ publisher: 'unknown' })[0].text, 'Unknown', 'unknown is a value')
  assert.equal(S.catalogFilterChips({ publisher: 'acme' })[0].text, 'acme', 'a publisher the facet does not list still reads')
  const f = { category: ['browser', 'runtime'], publisher: 'microsoft', unmatched: true, hidden: 'include' }
  assert.deepEqual(S.withoutChip(f, { key: 'category', value: 'browser' }).category, ['runtime'])
  assert.equal(S.withoutChip({ category: ['browser'] }, { key: 'category', value: 'browser' }).category, undefined)
  assert.equal(S.withoutChip(f, { key: 'publisher', value: 'microsoft' }).publisher, undefined)
  assert.equal(S.withoutChip(f, { key: 'unmatched', value: 'true' }).unmatched, undefined)
  assert.equal(S.withoutChip(f, { key: 'unmatched', value: 'true' }).hidden, 'include', 'removing a chip keeps the view mode')
}

// Sort rules: the default direction depends on the sort (counts biggest first, everything else A to Z).
{
  assert.equal(S.defaultOrderFor('hosts'), 'desc')
  assert.equal(S.defaultOrderFor('version_count'), 'desc')
  for (const k of ['name', 'publisher', 'category', 'latest_version', 'first_seen']) assert.equal(S.defaultOrderFor(k), 'asc', k)
  const d = S.readCatalogState(sp(''))
  assert.deepEqual([d.sort, d.order, d.page, d.pageSize], ['hosts', 'desc', 1, 50], 'defaults')
  // A sort named without a direction takes its own default (the shared helpers would say descending for every sort).
  assert.deepEqual(S.readCatalogState(sp('sort=name')).order, 'asc')
  assert.deepEqual(S.readCatalogState(sp('sort=version_count')).order, 'desc')
  assert.deepEqual(S.readCatalogState(sp('sort=name&order=desc')).order, 'desc')
  const bad = S.readCatalogState(sp('sort=installs&order=sideways&page=0&page_size=9999'))
  assert.deepEqual([bad.sort, bad.order, bad.page, bad.pageSize], ['hosts', 'desc', 1, 500], 'invalid values fall back; page_size is capped at the API maximum')
  assert.deepEqual(S.nextCatalogSort({ sort: 'hosts', order: 'desc' }, 'hosts'), { sort: 'hosts', order: 'asc' }, 'the same column flips')
  assert.deepEqual(S.nextCatalogSort({ sort: 'hosts', order: 'desc' }, 'name'), { sort: 'name', order: 'asc' }, 'a new text column starts A to Z')
  assert.deepEqual(S.nextCatalogSort({ sort: 'name', order: 'asc' }, 'version_count'), { sort: 'version_count', order: 'desc' }, 'a new count column starts biggest first')
  // The address leaves the default view out and always writes sort together with order otherwise.
  assert.equal(S.withCatalogSort(sp('q=chrome'), 'hosts').toString(), 'q=chrome&sort=hosts&order=asc')
  assert.equal(S.withCatalogSort(sp('q=chrome&sort=hosts&order=asc'), 'hosts').toString(), 'q=chrome', 'back to the default view: nothing written')
  assert.equal(S.withCatalogSort(sp('q=chrome'), 'name').toString(), 'q=chrome&sort=name&order=asc')
  assert.equal(S.withCatalogSort(sp('page=4&sort=name&order=asc'), 'name').toString(), 'sort=name&order=desc', 'a new sort goes back to page 1')
  // Paging keeps the sort and the filters; page 1 and the default size are not written.
  assert.equal(S.withCatalogPage(sp('sort=name&order=asc'), 3).toString(), 'sort=name&order=asc&page=3')
  assert.equal(S.withCatalogPage(sp('sort=name&order=asc&page=3'), 1).toString(), 'sort=name&order=asc')
  assert.equal(S.withCatalogPage(sp('sort=name'), 2).toString(), 'sort=name&order=asc&page=2', 'paging does not silently change the direction')
  assert.equal(S.withCatalogPageSize(sp('page=3'), 100).toString(), 'page_size=100')
  assert.equal(S.withCatalogPageSize(sp('page_size=100'), 50).toString(), '')
  assert.equal(S.withCatalogPageSize(sp(''), 9999).toString(), 'page_size=500')
  // New filters replace the old ones, written first; the page goes back to 1; other parameters stay.
  assert.equal(S.withCatalogFilters(sp('category=browser&page=3&sort=name&order=asc'), { q: 'edge' }).toString(), 'q=edge&sort=name&order=asc')
  // The request is explicit.
  assert.equal(S.catalogApiParams({ filters: { q: 'a', category: ['browser'] }, sort: 'hosts', order: 'desc', page: 1, pageSize: 50 }).toString(), 'q=a&category=browser&sort=hosts&order=desc&page=1&page_size=50')
  // A link into the catalog (a footer, a chip) is the same address the page writes.
  assert.equal(S.catalogHref({}), '/inventory/software')
  assert.equal(S.catalogHref({ hidden: 'include' }), '/inventory/software?hidden=include')
  assert.equal(S.catalogHref({ category: ['driver'], hidden: 'include' }), '/inventory/software?category=driver&hidden=include')
  assert.equal(S.catalogHref({ unmatched: true }), '/inventory/software?unmatched=true')
  assert.equal(S.catalogHref({ publisher: 'microsoft' }, { sort: 'name' }), '/inventory/software?publisher=microsoft&sort=name&order=asc')
  // Typing in the search box alone replaces the history entry; any other change pushes.
  assert.equal(S.catalogPushesHistory({ q: 'a' }, { q: 'ab' }), false)
  assert.equal(S.catalogPushesHistory({ q: 'a' }, { q: 'a', category: ['browser'] }), true)
  assert.equal(S.catalogPushesHistory({}, { hidden: 'include' }), true)
}

// The hide toggle: on = the default (never written), off = hidden=include.
{
  assert.equal(S.hideEnabled({}), true)
  assert.equal(S.hideEnabled({ hidden: 'include' }), false)
  assert.deepEqual(S.withHideToggle({ q: 'a', hidden: 'include' }, true), { q: 'a' })
  assert.deepEqual(S.withHideToggle({ q: 'a' }, false), { q: 'a', hidden: 'include' })
  assert.equal(S.writeCatalogFilters(S.withHideToggle({}, false)).toString(), 'hidden=include')
  assert.equal(S.writeCatalogFilters(S.withHideToggle({ hidden: 'include' }, true)).toString(), '')
  // Clearing the filters keeps how the list is viewed.
  assert.deepEqual(S.clearedFilters({ q: 'a', category: ['browser'], hidden: 'include' }), { hidden: 'include' })
  assert.deepEqual(S.clearedFilters({ q: 'a' }), {})
  assert.equal(S.catalogFilterCodec.count({ q: 'a', hidden: 'include', category: ['browser'] }), 1, 'the toggle and the search text are not chips')
}

/* ═══ (c) version bar, footer, drill links ══════════════════════════════════════════════════ */

{
  const git = CATALOG.items.find((r) => r.product_key === 'git')
  const bar = S.versionBarModel(git.product_key, git.distribution)
  assert.equal(bar.segments.length, 5)
  assert.deepEqual(bar.segments.map((s) => s.label), git.distribution.top.map((v) => v.version), "the server's order is kept (newest first within each scheme group)")
  assert.equal(bar.total, 19)
  assert(Math.abs(bar.segments.reduce((s, x) => s + x.width, 0) - 100) < 1e-9, 'the widths fill the bar')
  // Every version segment is a link to the assets with that version, and says its host count (principle 1).
  for (const [i, seg] of bar.segments.entries()) {
    const v = git.distribution.top[i]
    assert.equal(seg.hosts, v.hosts)
    assert.equal(seg.href, `/inventory/assets?product=git&product_version=${encodeURIComponent(v.version)}`, v.version)
    assert.equal(seg.text, `${v.version}: ${v.hosts} ${v.hosts === 1 ? 'host' : 'hosts'}`)
    assert(!/%/.test(seg.text), 'no percentage: a server running two versions counts in both')
  }
  assert.equal(bar.segments[3].href, '/inventory/assets?product=git&product_version=1%3A2.34.1-1ubuntu1.11', 'a dpkg epoch is escaped in the address')
  // The bucket of further versions is text: no list stands behind it.
  const more = S.versionBarModel('google-chrome', { top: [{ version: '152.0.8', hosts: 20, scheme: 'semverish', rank: 1 }], other: { versions: 6, hosts: 11 } })
  assert.deepEqual(more.segments.map((s) => [s.kind, s.label, s.hosts, s.href]), [['version', '152.0.8', 20, '/inventory/assets?product=google-chrome&product_version=152.0.8'], ['other', '6 other versions', 11, undefined]])
  assert.equal(more.segments[1].text, '6 other versions: 11 hosts across them')
  assert.equal(S.versionBarModel('x', { top: [], other: { versions: 0, hosts: 0 } }).segments.length, 0, 'a product with no version has no bar')
  assert.equal(S.versionBarModel('x', { top: [{ version: '1', hosts: 1, scheme: 'semverish', rank: 1 }], other: { versions: 1, hosts: 0 } }).segments.length, 1, 'an empty bucket is not drawn')
  assert.equal(S.versionBarLabel({ name: 'Git', version_count: 5, latest_version: '2.47.0' }), 'Git: 5 versions; newest 2.47.0')
  assert.equal(S.versionBarLabel({ name: 'Git', version_count: 1, latest_version: null }), 'Git: 1 version')
  assert.equal(S.versionBarLabel({ name: 'Zoom', version_count: 0, latest_version: null }), 'Zoom: no version reported')
  assert.equal(S.productPath('microsoft-visual-c-plus-plus-redistributable'), '/inventory/software/microsoft-visual-c-plus-plus-redistributable')
  assert.equal(S.productPath('a b/c'), '/inventory/software/a%20b%2Fc', 'the key is escaped as one path segment')
  assert.equal(S.shortDate('2026-07-19T12:00:00Z'), '2026-07-19')
  assert.equal(S.shortDate(null), '—')
  assert.equal(S.shortDate('not a date'), '—')
  assert.equal(S.productSubline({ publisher: 'Google', category: 'browser' }), 'Google · Browser')
  assert.equal(S.productSubline({ publisher: null, category: 'remote-access' }), 'Remote access')
}
{
  // The footer: the hidden products, the way to show them, and the unmatched titles.
  const resp = { total: 26, hidden_products: 5, hidden_installs: 153, unmatched_titles: 37, hidden_categories: ['driver', 'os-component', 'update'] }
  const f = S.catalogFooter(resp, {})
  assert.deepEqual(f.hidden, { products: 5, installs: 153, categories: resp.hidden_categories, showHref: '/inventory/software?hidden=include', showTotal: 31 }, 'the link text carries the number its list has: total + hidden products')
  assert.equal(f.showingHidden, null)
  assert.deepEqual(f.unmatched, { count: 37, href: '/inventory/software?unmatched=true' })
  // The links keep the other filters.
  const g = S.catalogFooter(resp, { q: 'micro', publisher: 'microsoft' })
  assert.equal(g.hidden.showHref, '/inventory/software?q=micro&publisher=microsoft&hidden=include')
  assert.equal(g.unmatched.href, '/inventory/software?q=micro&publisher=microsoft&unmatched=true')
  // Showing everything: nothing is hidden, and the way back is offered.
  const h = S.catalogFooter(resp, { hidden: 'include' })
  assert.equal(h.hidden, null)
  assert.deepEqual(h.showingHidden, { categories: resp.hidden_categories, hideHref: '/inventory/software' })
  // Nothing hidden and nothing unmatched: no footer notes. Unmatched only: no link to itself.
  const none = S.catalogFooter({ ...resp, hidden_products: 0, hidden_installs: 0, unmatched_titles: 0 }, {})
  assert.deepEqual([none.hidden, none.unmatched], [null, null])
  assert.equal(S.catalogFooter(resp, { unmatched: true }).unmatched, null)
  // No category is hidden by default (settings): the switch has nothing to say.
  assert.equal(S.catalogFooter({ ...resp, hidden_categories: [], hidden_products: 0 }, { hidden: 'include' }).showingHidden, null)
  // The golden catalog: the hidden total is the size of the list the Show link opens (contract section 4).
  const real = S.catalogFooter(CATALOG, {})
  assert.equal(real.hidden.showTotal, CATALOG.total + CATALOG.hidden_products)
}
{
  // Publisher options: the facet's, plus a selected value the facet does not list.
  assert.deepEqual(S.publisherOptions(PUBLISHERS, 'microsoft').map((o) => o.value).slice(0, 2), ['microsoft', 'google'])
  const extra = S.publisherOptions(PUBLISHERS, 'acme')
  assert.deepEqual(extra[0], { value: 'acme', label: 'acme', products: 0 }, 'a chosen publisher that the facet lacks stays visible')
  assert.equal(S.publisherOptions(PUBLISHERS, 'MICROSOFT').length, PUBLISHERS.items.length, 'case-insensitive: not added twice')
  assert.equal(S.publisherOptions(undefined, 'unknown')[0].label, 'Unknown')
  assert.equal(S.publisherOptions(undefined, undefined).length, 0)
  assert.deepEqual(S.publisherQueryFilters({ q: 'a', publisher: 'microsoft', hidden: 'include' }), { q: 'a', hidden: 'include' }, 'the facet leaves out the publisher filter itself')
  assert.equal(S.publisherNote(PUBLISHERS), null)
  assert.match(S.publisherNote({ ...PUBLISHERS, distinct: 340 }), /Showing the 21 publishers with the most products, of 340/)
  assert.equal(S.productRangeText(1, 50, 26), '1 to 26 of 26 products')
  assert.equal(S.productRangeText(1, 50, 0), '0 products')
}
{
  // Normalisation status: every status has words; unmatched is a value, not an error.
  for (const s of ['normalized', 'publisher_only', 'unmatched']) {
    const meta = S.normStatusMeta(s)
    assert(meta.label && meta.tone && meta.meaning, s)
  }
  assert.equal(S.normStatusMeta('weird').label, 'Unmatched', 'an unknown status reads as unmatched, never as healthy')
  assert.equal(S.normStatusMeta(null).label, 'Unmatched')
  assert.match(S.rowSummary({ ...CATALOG.items[0], norm_status: 'unmatched' }), /unmatched/)
  assert.doesNotMatch(S.rowSummary(CATALOG.items[0]), /normalized/, 'the normal case is not announced')
}

/* ═══ (d) the product page model ════════════════════════════════════════════════════════════ */

{
  assert.deepEqual([...P.PRODUCT_TABS], ['versions', 'installed', 'timeline', 'evidence'])
  assert.equal(P.parseProductTab(null), 'versions')
  assert.equal(P.parseProductTab('Installed'), 'installed')
  assert.equal(P.parseProductTab('lifecycle'), 'versions')
  const s = P.readProductState(sp('tab=installed&version=2.47.0&q=srv&sort=version&order=desc&page=3&page_size=100&days=30'))
  assert.equal(s.tab, 'installed')
  assert.deepEqual(s.installed, { version: '2.47.0', q: 'srv', sort: 'version', order: 'desc', page: 3, pageSize: 100 })
  assert.equal(s.days, 30)
  const d = P.readProductState(sp(''))
  assert.deepEqual([d.tab, d.installed.sort, d.installed.order, d.installed.page, d.installed.pageSize, d.days], ['versions', 'asset_name', 'asc', 1, 50, 7])
  const bad = P.readProductState(sp('tab=x&sort=colour&order=up&page=-1&days=45&version=%20'))
  assert.deepEqual([bad.tab, bad.installed.sort, bad.installed.version, bad.days], ['versions', 'asset_name', undefined, 7])
  // A tab is a different table: it starts clean.
  assert.equal(P.withProductTab(sp('tab=installed&version=1&q=a&sort=version&order=desc&page=2&page_size=100'), 'versions').toString(), '')
  assert.equal(P.withProductTab(sp('tab=installed&version=1&page=2'), 'timeline').toString(), 'tab=timeline')
  assert.equal(P.withProductTab(sp('days=30'), 'evidence').toString(), 'tab=evidence')
  // The Installed on list: filters, sort and paging in the address; defaults are not written.
  assert.equal(P.withInstalled(sp('tab=installed'), { version: '__none__' }).toString(), 'tab=installed&version=__none__')
  assert.equal(P.withInstalled(sp('tab=installed&version=1&page=3'), { q: 'srv' }).toString(), 'tab=installed&version=1&q=srv', 'a new filter goes back to page 1')
  assert.equal(P.withInstalled(sp('tab=installed&version=1&q=srv'), { version: undefined }).toString(), 'tab=installed&q=srv', 'undefined removes a filter')
  assert.equal(P.withInstalled(sp('tab=installed&page=2'), { page: 3 }, { keepPage: true }).toString(), 'tab=installed&page=3')
  assert.equal(P.withInstallSort(sp('tab=installed'), 'version').toString(), 'tab=installed&sort=version&order=asc')
  assert.equal(P.withInstallSort(sp('tab=installed&sort=version&order=asc'), 'version').toString(), 'tab=installed&sort=version&order=desc')
  assert.equal(P.withInstallSort(sp('tab=installed&sort=version&order=desc'), 'asset_name').toString(), 'tab=installed', 'back to the default order: nothing written')
  assert.equal(P.withInstallSort(sp('tab=installed'), 'updated_at').toString(), 'tab=installed&sort=updated_at&order=desc', 'a date column starts with the newest')
  assert.deepEqual(P.nextInstallSort({ sort: 'asset_name', order: 'asc' }, 'asset_name'), { sort: 'asset_name', order: 'desc' })
  assert.equal(P.withTimelineDays(sp('tab=timeline&page=3'), 30).toString(), 'tab=timeline&days=30')
  assert.equal(P.withTimelineDays(sp('tab=timeline&days=30'), 7).toString(), 'tab=timeline')
  assert.equal(P.withTimelinePage(sp('tab=timeline'), 2).toString(), 'tab=timeline&page=2')
  assert.equal(P.withTimelinePage(sp('tab=timeline&page=2'), 1).toString(), 'tab=timeline')
  // Links into the tabs.
  assert.equal(P.installedHref('git'), '/inventory/software/git?tab=installed')
  assert.equal(P.installedHref('git', '2.47.0'), '/inventory/software/git?tab=installed&version=2.47.0')
  assert.equal(P.installedHref('git', API.NO_VERSION), '/inventory/software/git?tab=installed&version=__none__', 'the rows with no version are addressed as __none__ (unversioned.address), not unknown')
  assert.equal(P.installedHref('git', 'unknown'), '/inventory/software/git?tab=installed&version=unknown', 'a version spelled unknown is an ordinary version')
  assert.equal(P.installedHref('git', '1:2.34.1-1ubuntu1.11'), '/inventory/software/git?tab=installed&version=1%3A2.34.1-1ubuntu1.11')
  assert.equal(P.versionsHref('git'), '/inventory/software/git')
  assert.equal(P.tabHref('git', 'timeline'), '/inventory/software/git?tab=timeline')
  assert.equal(P.tabHref('git', 'versions'), '/inventory/software/git')
  // The address round-trips into the state.
  const href = P.installedHref('git', '1:2.34.1-1ubuntu1.11')
  assert.equal(P.readProductState(new URL(href, 'http://x').searchParams).installed.version, '1:2.34.1-1ubuntu1.11')
}

// Version ranking labels: N, N-1, Older, and a version the scheme cannot order says so.
{
  const g = (generation, rank) => P.generationMeta({ generation, rank })
  assert.equal(g('N', 1).label, 'N')
  assert.equal(g('N-1', 2).label, 'N-1')
  assert.equal(g('older', 3).label, 'Older')
  assert.match(g('older', 3).title, /Two or more versions behind/, 'DT-18: the server rule is two or more')
  assert.equal(g(null, null).label, 'Unordered')
  assert.match(g(null, null).title, /cannot be ordered/)
  assert.equal(new Set(['N', 'N-1', 'older', null].map((x) => g(x, 1).tone)).size, 4, 'four states, four looks - and each has its own word')
  assert.match(g('N', 1).tone, /success-fg/)
  assert.match(g('older', 9).tone, /warning-fg/, 'status foreground tokens only (contrast-checked)')
  // The groups follow the server's order and its scheme grouping.
  const groups = P.versionGroups(PRODUCT.versions)
  assert.deepEqual(groups.map((x) => [x.scheme, x.versions.length, x.hosts]), [['semverish', 3, 13], ['dpkg', 2, 6]])
  assert.equal(P.versionGroups([]).length, 0)
  assert.equal(P.SCHEME_LABELS.dpkg.title, 'Debian and Ubuntu packages')
  // Rows: the chart and its table twin share them; every number links to its list.
  const rows = P.versionRows('git', PRODUCT.versions)
  assert.equal(rows.length, PRODUCT.versions.length)
  assert.deepEqual(rows.map((r) => r.generation.label), ['N', 'N-1', 'Older', 'N', 'N-1'], 'each scheme group has its own N')
  assert.equal(rows[0].hostsHref, '/inventory/assets?product=git&product_version=2.47.0')
  assert.equal(rows[0].installsHref, '/inventory/software/git?tab=installed&version=2.47.0')
  assert.equal(rows[3].hostsHref, '/inventory/assets?product=git&product_version=1%3A2.34.1-1ubuntu1.11')
  assert.equal(Math.max(...rows.map((r) => r.width)), 100, 'the busiest version fills its bar')
  assert(rows.every((r) => r.width > 0 && r.width <= 100))
  assert.equal(rows[1].hosts, 6)
  assert.equal(P.versionRows('x', []).length, 0)
}
// KPIs: hosts, versions, latest, behind latest.
{
  const k = Object.fromEntries(P.productKpis(PRODUCT).map((x) => [x.id, x]))
  assert.deepEqual(Object.keys(k), ['hosts', 'versions', 'latest', 'behind'])
  assert.deepEqual([k.hosts.value, k.hosts.href, k.hosts.count], ['19', '/inventory/assets?product=git', 19], 'hosts opens the assets with the product')
  assert.deepEqual([k.versions.value, k.versions.href, k.versions.count], ['5', '/inventory/software/git', 5], 'versions opens the Versions tab, which has that many rows')
  assert.deepEqual([k.latest.value, k.latest.href, k.latest.count], ['2.47.0', '/inventory/assets?product=git&product_version=2.47.0', 5])
  assert.equal(k.latest.sub, '5 hosts on it')
  // Mixed schemes: a Windows host and an Ubuntu host are not behind the same release, so there is no figure.
  assert.deepEqual([k.behind.value, k.behind.href, k.behind.count], ['—', null, null])
  assert.match(k.behind.sub, /ranked separately/)
  assert(k.behind.note, 'the tile says why it has no link')
  // One scheme: hosts without the newest version, as text (no list stands behind it).
  const one = {
    header: { ...PRODUCT.header, mixed_schemes: false, hosts: 41, installs: 41, version_count: 3, latest_version: '152.0.8001.12', product_key: 'google-chrome' },
    versions: [
      { version: '152.0.8001.12', hosts: 20, installs: 20, scheme: 'semverish', rank: 1, generation: 'N' },
      { version: '151.0.7922.110', hosts: 14, installs: 14, scheme: 'semverish', rank: 2, generation: 'N-1' },
      { version: '150.0.7871.50', hosts: 7, installs: 7, scheme: 'semverish', rank: 3, generation: 'older' },
    ],
    unversioned: { hosts: 0, installs: 0 },
  }
  assert.equal(P.behindLatest(one.header, one.versions), 21)
  const k1 = Object.fromEntries(P.productKpis(one).map((x) => [x.id, x]))
  assert.deepEqual([k1.behind.value, k1.behind.href, k1.behind.count], ['21', null, null], 'plain text: the filters cannot say "not this version"')
  assert.equal(k1.hosts.sub, 'servers with it installed')
  // A host that runs two versions counts once: hosts minus the hosts on the newest.
  assert.equal(P.behindLatest({ ...one.header, hosts: 30 }, one.versions), 10)
  assert.equal(P.behindLatest({ ...one.header, hosts: 5 }, one.versions), 0, 'never negative')
  // No orderable version: unknown, not zero.
  assert.equal(P.behindLatest({ ...one.header, latest_version: null }, one.versions), null)
  const kn = Object.fromEntries(P.productKpis({ ...one, header: { ...one.header, latest_version: null, version_count: 0 }, versions: [] }).map((x) => [x.id, x]))
  assert.deepEqual([kn.latest.value, kn.latest.href, kn.behind.value], ['—', null, '—'])
  assert.equal(kn.latest.sub, 'no version can be ordered')
  // Unversioned installs are mentioned, never folded into a version.
  assert.match(Object.fromEntries(P.productKpis({ ...one, unversioned: { hosts: 2, installs: 3 } }).map((x) => [x.id, x])).versions.sub, /plus 3 installations with none reported/)
  assert.match(Object.fromEntries(P.productKpis({ ...one, header: { ...one.header, installs: 50 } }).map((x) => [x.id, x])).hosts.sub, /50 installations on 41 servers/)
  assert.equal(P.productFactLine(PRODUCT.header, S.shortDate), 'First seen 2026-07-19 · last change 2026-10-05')
  assert.equal(P.productFactLine({ first_seen: null, last_change: null }, S.shortDate), 'First seen unknown · no change recorded yet')
}
// The version select of the Installed on tab.
{
  const opts = P.versionFilterOptions({ ...PRODUCT, unversioned: { hosts: 1, installs: 2, address: '__none__' } }, undefined)
  assert.equal(opts.length, 6)
  assert.deepEqual(opts.at(-1), { value: '__none__', label: 'No version reported', installs: 2 }, 'the rows with no version are a value of the select, addressed by unversioned.address')
  assert.deepEqual(opts[0], { value: '2.47.0', label: '2.47.0', installs: 5 })
  assert.equal(P.versionFilterOptions(PRODUCT, undefined).length, 5, 'no unversioned rows: no such option')
  assert.equal(P.versionFilterOptions(PRODUCT, '9.9.9')[0].value, '9.9.9', 'a version from the address stays selectable')
  assert.equal(P.versionFilterLabel(undefined), 'All versions')
  assert.equal(P.versionFilterLabel('__none__'), 'No version reported')
  assert.equal(P.versionFilterLabel('unknown'), 'unknown', 'a version spelled unknown is an ordinary version')
  assert.equal(P.installedSummary(19, 19, undefined), '19 installations')
  assert.equal(P.installedSummary(5, 19, '2.47.0'), '5 installations with version 2.47.0 (of 19)')
  assert.equal(P.installedSummary(1, 19, '__none__'), '1 installation with no version reported (of 19)')
  assert.equal(P.installedSummary(1, 19, 'unknown'), '1 installation with version unknown (of 19)')
  assert.equal(P.installSourceLabel('hklm'), 'HKLM')
  assert.equal(P.installSourceLabel('dpkg'), 'dpkg')
  assert.equal(P.installSourceLabel(null), 'Unknown')
  assert.equal(P.installSourceLabel('winget'), 'Winget')
  assert.equal(P.installSourceLabel('hklm_wow6432'), 'HKLM (32-bit)', 'DT-07: one label helper for every page')
  assert.equal(P.installAssetHref(INSTALLS.items[0]), `/inventory/assets?asset=${INSTALLS.items[0].asset_key}`, 'a server opens in the asset flyout')
}
// The timeline: the rows are the Changes page's own list and its pairing rule (an installation and a removal
// written together are ONE upgrade); this page adds the window, the statistics and the links.
{
  const row = (over) => ({
    id: 1, observed_at: '2026-10-08T01:43:00Z', asset_key: 'server:s1', asset_kind: 'server', asset_id: 's1', asset_name: 'srv-w22-17', asset_present: true, server_id: null,
    category: 'software', action: 'added', subject: 'Java 8 Update 401', subject_key: 'java-se-runtime', old_value: null, new_value: '8.0.4010.12', label: null, pair: null,
    marker: false, snapshot_truncated: false, detail: {}, ...over,
  })
  const added = row({ id: 51, pair: { event_id: 52, label: 'upgrade' } })
  const removed = row({ id: 52, action: 'removed', subject: 'Java 8 Update 391', old_value: '8.0.3910.11', new_value: null, pair: { event_id: 51, label: 'upgrade' } })
  const plain = row({ id: 50, action: 'changed', subject: 'Java 8', old_value: '8.0.351', new_value: '8.0.391', label: 'upgrade', observed_at: '2026-10-07T10:00:00Z' })
  const built = CH.buildEntries([removed, added, plain])
  assert.equal(built.entries.length, 2, 'the two halves are one row')
  assert.equal(built.entries[0].kind, 'pair')
  assert.match(built.entries[0].sentence, /upgraded 8\.0\.3910\.11 → 8\.0\.4010\.12/)
  // The golden change feed through the same code: every software event is shown exactly once.
  const feed = fixture('changes').items.filter((r) => r.category === 'software')
  const { entries, merged } = CH.buildEntries(feed)
  assert(merged > 0, 'the golden feed has paired upgrades')
  assert.equal(entries.length, feed.length - merged, 'each pair removes exactly one row')
  assert.deepEqual(entries.flatMap((e) => e.ids).sort((a, b) => a - b), feed.map((r) => r.id).sort((a, b) => a - b), 'every event is shown exactly once')
  const days = CH.groupByDay(entries, 'UTC', Date.parse('2026-10-08T12:00:00Z'))
  assert.equal(days.reduce((n, g) => n + g.entries.length, 0), entries.length)
  assert.equal(typeof CH.browserTimeZone(), 'string')
  // The window and the statistics' links to the Changes page.
  assert.equal(P.timelineSince(7, Date.parse('2026-10-08T12:34:56Z')), '2026-10-01T12:00:00Z', 'rounded down to the hour: the query key is stable')
  assert.equal(P.timelineSince(30, Date.parse('2026-10-08T12:00:00Z')), '2026-09-08T12:00:00Z')
  const stats = P.timelineStats('git', { software: { added: 3, removed: 2, changed: 5, upgraded: 4, downgraded: 1 } }, '2026-10-01T12:00:00Z')
  assert.deepEqual(stats.map((s) => [s.label, s.value]), [['installed', 3], ['removed', 2], ['upgraded', 4], ['downgraded', 1]])
  assert.equal(stats[2].href, '/inventory/changes?category=software&action=changed&label=upgrade&subject_key=git&since=2026-10-01T12%3A00%3A00Z', 'one builder (changesPageHref), so the canonical parameter order')
  assert.equal(stats[2].href, CH.changesPageHref({ subject_key: 'git', category: 'software', action: 'changed', label: 'upgrade', since: '2026-10-01T12:00:00Z' }), 'FE-04: productChangesHref delegates to changesPageHref')
  assert.equal(P.productChangesHref('git'), '/inventory/changes?subject_key=git')
  assert.deepEqual(P.timelineStats('git', undefined, 'x'), [])
  // The links are valid addresses of the Changes page's own vocabulary (it reads the same names).
  const back = CH.parseChangesFilters(new URL(stats[2].href, 'http://x').searchParams)
  assert.deepEqual(back.rejected, [])
  assert.deepEqual(back.filters, { subject_key: 'git', category: 'software', action: 'changed', label: 'upgrade', since: '2026-10-01T12:00:00Z' })
  // Page size and page of the timeline live in the address.
  assert.equal(P.readProductState(sp('tab=timeline&page=2&page_size=100')).pageSize, 100)
  assert.equal(P.withTimelinePageSize(sp('tab=timeline&page=3'), 100).toString(), 'tab=timeline&page_size=100')
  assert.equal(P.withTimelinePageSize(sp('tab=timeline&page_size=100'), 50).toString(), 'tab=timeline')
}
// Evidence and the not-found state.
{
  const t = (status, rule_id) => P.evidenceStatusText({ status, rule_id })
  assert.equal(t('normalized', 'git'), 'Rule git')
  assert.equal(t('normalized', null), 'Matched by a rule')
  assert.match(t('publisher_only', null), /Publisher recognised/)
  assert.equal(t('unmatched', null), 'No rule matched')
  assert.equal(t(null, null), 'Not evaluated')
  assert.equal(P.evidenceNote(PRODUCT.evidence), null)
  assert.match(P.evidenceNote({ items: new Array(200).fill({}), total: 340, truncated: true }), /200 most widely installed of 340/)
  assert.equal(P.isNotFound({ response: { status: 404 } }), true)
  assert.equal(P.isNotFound({ response: { status: 500 } }), false)
  assert.equal(P.isNotFound(new Error('x')), false)
  assert.equal(P.isNotFound(null), false)
}

/* ═══ (e) the settings model ═══════════════════════════════════════════════════════════════ */

{
  const cur = SETTINGS.settings
  const draft = T.draftFromSettings(cur)
  assert.deepEqual(draft, { fresh_server: '24', fresh_device: '2', stale_days: '7', change_retention_days: '400', snapshot_hour: '0', hide: ['update', 'driver', 'os-component', 'runtime'], trends_enabled: true })
  const ok = T.validateDraft(draft, SETTINGS.limits)
  assert.deepEqual(ok.errors, {})
  assert.deepEqual(ok.values, cur, 'the untouched draft stands for the current settings')
  assert.equal(T.draftDirty(draft, cur), false)
  assert.deepEqual(T.diffSettings(cur, ok.values), {}, 'nothing changed: nothing to send')
  assert(T.isEmptyPatch(T.diffSettings(cur, ok.values)))

  // Inline validation, one message per field, with the limits in it.
  const check = (patch) => T.validateDraft({ ...draft, ...patch }, SETTINGS.limits)
  assert.equal(check({ fresh_server: '' }).errors.fresh_server, 'Enter a whole number from 1 to 720.')
  assert.equal(check({ fresh_server: '0' }).errors.fresh_server, 'Must be a whole number from 1 to 720.')
  assert.equal(check({ fresh_server: '721' }).errors.fresh_server, 'Must be a whole number from 1 to 720.')
  assert.equal(check({ fresh_device: '1.5' }).errors.fresh_device, 'Enter a whole number from 1 to 720; no decimals, signs or units.')
  assert.match(check({ stale_days: '-3' }).errors.stale_days, /no decimals, signs or units/)
  assert.match(check({ stale_days: '7 days' }).errors.stale_days, /no decimals, signs or units/)
  assert.match(check({ stale_days: '366' }).errors.stale_days, /from 1 to 365/)
  assert.match(check({ change_retention_days: '29' }).errors.change_retention_days, /from 30 to 3,650/)
  assert.match(check({ change_retention_days: '3651' }).errors.change_retention_days, /from 30 to 3,650/)
  assert.match(check({ snapshot_hour: '24' }).errors.snapshot_hour, /from 0 to 23/)
  assert.deepEqual(check({ snapshot_hour: '23' }).errors, {}, 'the limits themselves are valid')
  assert.deepEqual(check({ snapshot_hour: '0', stale_days: '1', fresh_server: '1' }).errors, {})
  assert.equal(check({ fresh_server: ' 12 ' }).values.fresh_hours.server, 12, 'spaces around a number are fine')
  assert.equal(check({ fresh_server: '12' }).errors.fresh_server, undefined)
  // A fresh target never above stale days x 24 (the server would lower it): said before saving.
  assert.match(check({ stale_days: '1', fresh_server: '48' }).errors.fresh_server, /At most 24 hours/)
  assert.deepEqual(check({ stale_days: '1', fresh_server: '24', fresh_device: '24' }).errors, {})
  assert.match(check({ stale_days: '2', fresh_device: '49' }).errors.fresh_device, /At most 48 hours.*2 x 24/)
  assert.equal(check({ stale_days: 'x', fresh_server: '48' }).errors.fresh_server, undefined, 'no cross check against a stale days that is not valid yet')
  assert.equal(check({ stale_days: '' }).values, null, 'any error: nothing to save')
  assert.equal(Object.keys(check({ fresh_server: '', fresh_device: 'a', stale_days: '0' }).errors).length, 3, 'every bad field is flagged at once')

  // The PUT body holds only what changed.
  const next = (patch) => T.diffSettings(cur, check(patch).values)
  assert.deepEqual(next({ stale_days: '14' }), { stale_days: 14 })
  assert.deepEqual(next({ fresh_device: '4' }), { fresh_hours: { device: 4 } }, 'fresh_hours names only the kind that changed')
  assert.deepEqual(next({ fresh_server: '48', fresh_device: '4' }), { fresh_hours: { server: 48, device: 4 } })
  assert.deepEqual(next({ change_retention_days: '90', snapshot_hour: '3' }), { change_retention_days: 90, snapshot_hour: 3 })
  assert.deepEqual(next({ trends_enabled: false }), { trends_enabled: false })
  assert.deepEqual(next({ hide: ['update'] }), { hide_categories_by_default: ['update'] })
  assert.deepEqual(next({ hide: [] }), { hide_categories_by_default: [] }, 'hiding nothing is a change, not "no change"')
  assert.deepEqual(next({ hide: ['runtime', 'os-component', 'update', 'driver'] }), {}, 'the same categories in another order are no change')
  assert.deepEqual(next({ fresh_server: '24', stale_days: '7' }), {}, 'typing the current value back is no change')
  assert.deepEqual(next({ snapshot_hour: '00' }), {}, '"00" is 0')
  assert.equal(T.draftDirty({ ...draft, stale_days: '8' }, cur), true)
  assert.equal(T.draftDirty({ ...draft, stale_days: '' }, cur), true, 'a half-typed value counts as unsaved')
  assert.equal(T.draftDirty({ ...draft, hide: ['driver', 'update', 'runtime', 'os-component'] }, cur), false)
  assert.equal(T.draftDirty({ ...draft, trends_enabled: false }, cur), true)

  // The hidden-categories checkboxes keep the order of the vocabulary and only known categories.
  const vocab = SETTINGS.categories
  assert.deepEqual(T.toggleHidden(['update'], 'browser', true, vocab), ['browser', 'update'])
  assert.deepEqual(T.toggleHidden(['browser', 'update'], 'browser', false, vocab), ['update'])
  assert.deepEqual(T.toggleHidden([], 'driver', false, vocab), [])
  assert.deepEqual(T.toggleHidden(['update'], 'boots', true, vocab), ['update'], 'a category outside the vocabulary is not added')
  assert.deepEqual(T.toggleHidden(['update', 'driver', 'os-component'], 'driver', false, vocab), ['os-component', 'update'])
  assert.match(T.hiddenSummary(['driver', 'update']), /Hidden by default: Update, Driver\./, 'named in the order people say them')
  // UI-03, DT-08: the sentence quotes the switch the catalog shows, built from the same list.
  assert.match(T.hiddenSummary(['driver', 'update']), /Switching off "Hide updates and drivers" in the catalog/)
  assert.match(T.hiddenSummary(cur.hide_categories_by_default), /Switching off "Hide updates, drivers, OS components and runtimes"/)
  assert(!T.hiddenSummary(['driver']).includes('runtimes, drivers and updates'), 'no fixed wording')
  assert.match(T.hiddenSummary([]), /Nothing is hidden by default/)
  // Help text states the limit and the default.
  assert.equal(T.helpFor('fresh_server', SETTINGS.limits, SETTINGS.defaults), '1 to 720 hours. Default 24.')
  assert.equal(T.helpFor('fresh_device', SETTINGS.limits, SETTINGS.defaults), '1 to 720 hours. Default 2.')
  assert.equal(T.helpFor('stale_days', SETTINGS.limits, SETTINGS.defaults), '1 to 365 days. Default 7.')
  assert.equal(T.helpFor('change_retention_days', SETTINGS.limits, SETTINGS.defaults), '30 to 3,650 days. Default 400.')
  assert.equal(T.helpFor('snapshot_hour', SETTINGS.limits, SETTINGS.defaults), "0 to 23 o'clock, appliance time. Default 0.")
  assert.match(T.freshnessPreview(cur), /A server is Fresh for 24 h.*Stale after 7 days.*device is Fresh for 2 h/)
  for (const f of T.DRAFT_FIELDS) assert(T.FIELD_LABELS[f], f)
  assert.deepEqual(T.limitFor('fresh_device', SETTINGS.limits), { min: 1, max: 720 })

  // The toast names the effect.
  const saved = { ...cur, stale_days: 14, hide_categories_by_default: ['update'], change_retention_days: 90, snapshot_hour: 3, trends_enabled: false }
  const toast = T.saveToast({ changed: ['stale_days', 'hide_categories_by_default', 'change_retention_days', 'snapshot_hour', 'trends_enabled'], adjusted: [], settings: saved })
  assert.equal(toast.kind, 'success')
  assert.equal(toast.title, 'Settings saved')
  assert.match(toast.description, /anything older than 14 days as stale/)
  assert.match(toast.description, /leave out Update/)
  assert.match(toast.description, /older than 90 days is removed on the next cleanup pass/)
  assert.match(toast.description, /03:15/)
  assert.match(toast.description, /Trends are hidden on the Overview; snapshots are still recorded/)
  assert.match(T.saveToast({ changed: ['hide_categories_by_default'], adjusted: [], settings: { ...cur, hide_categories_by_default: [] } }).description, /include every category/)
  assert.match(T.saveToast({ changed: ['trends_enabled'], adjusted: [], settings: cur }).description, /shown on the Overview again/)
  const none = T.saveToast({ changed: [], adjusted: [], settings: cur })
  assert.deepEqual([none.kind, none.title], ['info', 'Nothing to change'])
  const adj = T.saveToast({ changed: ['stale_days'], adjusted: ['stale_days', 'fresh_hours.server'], settings: { ...cur, stale_days: 365 } })
  assert.equal(adj.kind, 'warning')
  assert.match(adj.description, /adjusted stale days, the server fresh target/)
  assert.equal(T.adjustedText([]), null)
  assert.equal(T.formStatus({ dirty: false, invalid: false, saving: false }), 'All changes saved')
  assert.equal(T.formStatus({ dirty: true, invalid: false, saving: false }), 'Unsaved changes')
  assert.equal(T.formStatus({ dirty: true, invalid: true, saving: false }), 'Fix the highlighted fields to save')
  assert.equal(T.formStatus({ dirty: true, invalid: false, saving: true }), 'Saving')
}


/* ═══ (f) the components, rendered to markup ═══════════════════════════════════════════════ */

const h = m.createElement
const decode = (t) => t.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#x27;/g, "'")
const strip = (html) => decode(html.replace(/<[^>]*>/g, '')).replace(/\s+/g, ' ').trim()
const anchors = (html) => [...html.matchAll(/<a\s([^>]*)>/g)].map((a) => {
  const attrs = Object.fromEntries([...a[1].matchAll(/([\w:-]+)="([^"]*)"/g)].map((x) => [x[1], decode(x[2])]))
  return { href: attrs.href, attrs }
})
const USER = (permissions) => ({ id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions })
function setUser(user) {
  // Server rendering reads a zustand store's initial state, so the user goes there as well as into the live state.
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const VIEW = ['inventory.view']
// retryOnMount off: a query in the error state stays an error when it renders (otherwise it is shown as loading while it retries).
const newClient = () => new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
const renderAt = (qc, url, path, element) =>
  m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: [url] }, h(m.Routes, null, h(m.Route, { path, element })))))
const failQuery = (qc, key, error) => qc.getQueryCache().build(qc, { queryKey: key }).setState({ status: 'error', error, data: undefined, fetchStatus: 'idle', errorUpdateCount: 1 })

// The catalog page, from the golden response.
function catalogPage(url, { permissions = VIEW, catalog = CATALOG, publishers = PUBLISHERS, fail, publishersFail } = {}) {
  setUser(USER(permissions))
  const qc = newClient()
  const state = S.readCatalogState(new URL(url, 'http://x').searchParams)
  const key = API.softwareKeys.catalog(S.catalogApiParams(state).toString())
  if (fail) failQuery(qc, key, fail)
  else qc.setQueryData(key, catalog)
  const pubKey = API.softwareKeys.publishers(JSON.stringify({ ...state.filters, publisher: undefined }))
  if (publishersFail) failQuery(qc, pubKey, publishersFail)
  else qc.setQueryData(pubKey, publishers)
  return renderAt(qc, url, 'inventory/software', h(m.InventorySoftwarePage))
}
{
  const html = catalogPage('/inventory/software')
  const rows = [...html.matchAll(/data-product-row="([^"]+)"/g)].map((x) => x[1])
  assert.deepEqual(rows, CATALOG.items.map((r) => r.product_key), 'one row per product, in the server order')
  const N = CATALOG.total
  assert(html.includes(`<caption class="sr-only">Software, 1 to ${N} of ${N} products, sorted by hosts descending</caption>`))
  assert((html.match(/<th scope="col"/g) ?? []).length >= 3 && html.includes('aria-sort="descending"'), 'real table: scope headers and aria-sort')
  assert.equal((html.match(/aria-sort="(ascending|descending)"/g) ?? []).length, 1, 'one sorted column')
  assert(html.includes(`${N}</strong> products`))
  // Principle 1: each number links to the list behind it and says how many it is.
  const links = anchors(html)
  for (const r of CATALOG.items) {
    const hosts = links.find((l) => l.href === `/inventory/assets?product=${encodeURIComponent(r.product_key)}` && l.attrs['data-drill'] === 'assets' && l.attrs['data-drill-count'] === String(r.hosts))
    assert(hosts, `${r.product_key}: the host count links to the assets that have it`)
    assert(links.some((l) => l.href === `/inventory/software/${encodeURIComponent(r.product_key)}?tab=installed` && l.attrs['data-drill-count'] === String(r.installs)), `${r.product_key}: installs`)
    if (r.version_count > 0) assert(links.some((l) => l.href === `/inventory/software/${encodeURIComponent(r.product_key)}` && l.attrs['data-drill-count'] === String(r.version_count)), `${r.product_key}: versions`)
    for (const v of r.distribution.top) {
      assert(links.some((l) => l.href === `/inventory/assets?product=${encodeURIComponent(r.product_key)}&product_version=${encodeURIComponent(v.version)}` && l.attrs['data-drill-count'] === String(v.hosts)), `${r.product_key} ${v.version}: bar segment`)
    }
  }
  // The bar's segments are pointer targets in a table of many rows: out of the tab order, still named.
  const top0 = CATALOG.items.find((r) => r.distribution.top.length > 0).distribution.top[0]
  const segment = anchors(html).find((l) => l.attrs['aria-label']?.startsWith(`${top0.version}: ${top0.hosts} host`))
  assert(segment && segment.attrs.tabindex === '-1', 'a bar segment is not a Tab stop')
  // One Tab stop per table: only the first row's links are in the tab order.
  const tableHtml = html.slice(html.indexOf('<table'), html.indexOf('</table>'))
  const stops = anchors(tableHtml).filter((l) => l.attrs.tabindex === undefined || l.attrs.tabindex === '0')
  assert.equal(stops.length, 4, 'roving tab stop: only the first row\'s name, hosts, installs and versions links are in the tab order')
  const firstKey = encodeURIComponent(CATALOG.items[0].product_key)
  assert(stops.every((l) => l.attrs['data-product-link'] === CATALOG.items[0].product_key || l.href.includes(`product=${firstKey}`) || l.href.includes(`software/${firstKey}`)))
  // The hide switch is on (the default), the footer says what is left out and offers the way to see it.
  assert(/role="switch" aria-checked="true"/.test(html), 'the hide switch is on by default')
  assert(html.includes('Hide updates, drivers, OS components and runtimes'), 'UI-03: the switch is named after the categories the settings hide')
  assert(!html.includes('Hide runtimes, drivers and updates'))
  const all = N + CATALOG.hidden_products
  assert(html.includes(`${CATALOG.hidden_products} products`) && html.includes(`${CATALOG.hidden_installs} installations`) && html.includes(`Show all ${all}`))
  assert(links.some((l) => l.href === '/inventory/software?hidden=include' && l.attrs['data-drill-count'] === String(all)))
  // UI-03, DT-08: the footer names the same categories as the switch, in the same order.
  assert(strip(html).includes('in hidden categories (Update, Driver, OS component, Runtime) not listed'), 'the footer follows the settings')
  // Export only for a user who holds inventory.export.
  assert(!html.includes('aria-label="Export CSV"'))
  assert(catalogPage('/inventory/software', { permissions: [...VIEW, 'inventory.export'] }).includes('aria-label="Export CSV"'))
  // Names the product page, and the Publisher select shows its facet.
  assert(anchors(html).some((l) => l.href === '/inventory/software/google-chrome'))
  assert(html.includes('aria-label="Publisher: All publishers"') && html.includes('aria-label="Category: All categories"'))
}
{
  // Filters from the address: chips, the switch, the toolbar and the request all agree.
  const html = catalogPage('/inventory/software?category=browser&category=security&publisher=google&unmatched=true&hidden=include&q=chrome&sort=name&order=asc&page=1')
  const chips = anchors(html).filter((l) => (l.attrs['aria-label'] ?? '').startsWith('Remove filter'))
  assert.deepEqual(chips.map((c) => c.attrs['aria-label']), ['Remove filter Category: Browser', 'Remove filter Category: Security', 'Remove filter Publisher: Google', 'Remove filter Status: Unmatched only'])
  // A chip removes exactly one filter and keeps the rest (and the sort).
  assert.equal(chips[0].href, '/inventory/software?q=chrome&category=security&publisher=google&hidden=include&unmatched=true&sort=name&order=asc')
  assert.equal(chips[3].href, '/inventory/software?q=chrome&category=browser&category=security&publisher=google&hidden=include&sort=name&order=asc')
  assert(/role="switch" aria-checked="false"/.test(html), 'hidden=include: the switch is off')
  assert(html.includes('value="chrome"') && html.includes('Clear all'))
  assert(html.includes('Including the categories hidden by default') && html.includes('Hide them again'))
  assert(html.includes('aria-sort="ascending"'))
  // A scoped caller's numbers are the scoped response's.
  const scoped = catalogPage('/inventory/software', { catalog: CATALOG_SCOPED })
  assert(scoped.includes(`${CATALOG_SCOPED.total}</strong> products`))
  // An address the page cannot use is reported, not obeyed.
  const bad = catalogPage('/inventory/software?category=boots&sort=installs')
  assert(bad.includes('Ignored in the address') && bad.includes('category=boots (not a software category)'))
}
{
  // The states of doc 03 section 5.
  const empty = { ...CATALOG, items: [], total: 0, hidden_products: 0, hidden_installs: 0, unmatched_titles: 0 }
  const noData = catalogPage('/inventory/software', { catalog: empty })
  assert(noData.includes('No software inventory yet'), 'no data yet')
  // UI-02, UP-17, DT-09: only the Windows agent reports software; the page must not promise Linux packages.
  assert(strip(noData).includes("A server's software list arrives with the Windows agent's first inventory report. Linux servers and servers without the agent do not report software yet."))
  assert(!/Linux servers report|the same way|fills in by itself/.test(strip(noData)), 'no claim that Linux servers report packages')
  // X2: rows the normaliser has not keyed yet are not an empty estate.
  const NORM = { normalising: true, pending_software_rows: 1200, pending_identity_rows: 0 }
  const building = catalogPage('/inventory/software', { catalog: { ...empty, data_status: NORM } })
  assert(strip(building).includes('Normalising existing software lists; this page fills in over the next minutes'), 'the empty catalog says it is being built')
  assert(!building.includes('No software inventory yet') && !building.includes('Windows agent'), 'and does not claim the estate has no software')
  assert(strip(catalogPage('/inventory/software?q=zzz', { catalog: { ...empty, data_status: NORM } })).includes('still being normalised, so the catalog is incomplete'), 'a filter that matches nothing says the data is partial')
  assert(!strip(catalogPage('/inventory/software?q=zzz', { catalog: empty })).includes('still being normalised'))
  const noMatch = catalogPage('/inventory/software?q=zzz', { catalog: empty })
  assert(noMatch.includes('Nothing matches these filters') && noMatch.includes('Clear all filters'), 'the filters excluded everything: the way back')
  const hiddenOnly = catalogPage('/inventory/software', { catalog: { ...empty, hidden_products: 5, hidden_installs: 20 } })
  assert(hiddenOnly.includes('Every product is in a hidden category') && hiddenOnly.includes('Show them'))
  const scopedEmpty = catalogPage('/inventory/software', { catalog: { ...empty, scope: { restricted: true, tags: ['team-edge'] } } })
  assert(scopedEmpty.includes('Nothing in your scope') && scopedEmpty.includes('team-edge'))
  const beyond = catalogPage('/inventory/software?page=9', { catalog: { ...CATALOG, items: [], page: 9 } })
  assert(beyond.includes(`Page ${(9).toLocaleString()} is past the end`) && beyond.includes(`Go to the last page (${(1).toLocaleString()})`), 'numbers as the browser prints them, in any locale (NF-EP-06)')
  const failed = catalogPage('/inventory/software', { fail: Object.assign(new Error('x'), { response: { status: 500, data: {} } }) })
  assert(failed.includes('Could not load the software catalog') && failed.includes('Retry'))
  // Partial failure: the facet fails on its own, the catalog still renders (doc 03 section 5).
  const partial = catalogPage('/inventory/software', { publishersFail: Object.assign(new Error('x'), { response: { status: 500, data: {} } }) })
  assert(partial.includes('data-publishers-failed') && partial.includes('The publisher list could not be loaded') && partial.includes('data-software-table'))
  const forbidden = catalogPage('/inventory/software', { fail: Object.assign(new Error('x'), { response: { status: 403, data: { detail: 'no' } } }) })
  assert(forbidden.includes('Your role cannot view the software catalog') && !forbidden.includes('Retry'))
  // Loading: a skeleton, not an empty list; no request until the permissions are known or when they say no.
  setUser(USER(VIEW))
  const loading = renderAt(newClient(), '/inventory/software', 'inventory/software', h(m.InventorySoftwarePage))
  assert(loading.includes('aria-label="Loading software"') && !loading.includes('No software inventory yet'))
  setUser(USER([]))
  assert.equal(renderAt(newClient(), '/inventory/software', 'inventory/software', h(m.InventorySoftwarePage)), '', 'denied: the layout has said so, the page renders nothing')
}

// The product page.
function productPage(url, { permissions = VIEW, product = PRODUCT, fail, installs } = {}) {
  setUser(USER(permissions))
  const qc = newClient()
  const key = API.softwareKeys.product('git')
  if (fail) failQuery(qc, key, fail)
  else qc.setQueryData(key, product)
  if (installs) {
    const st = P.readProductState(new URL(url, 'http://x').searchParams).installed
    const query = { version: st.version, q: st.q, sort: st.sort, order: st.order, page: st.page, page_size: st.pageSize }
    qc.setQueryData(API.softwareKeys.installations('git', JSON.stringify(query)), installs)
  }
  return renderAt(qc, url, 'inventory/software/:productKey', h(m.InventoryProductPage))
}
{
  const html = productPage('/inventory/software/git')
  assert(html.includes('<h1') && html.includes('>Git</h1>') && html.includes('Normalized') && html.includes('First seen 2026-07-19'))
  assert(html.includes('git</code>'), 'the product key is shown: it is the address and the filter value')
  const links = anchors(html)
  // The four figures: hosts and latest link to assets, versions to the Versions tab, behind latest has no list.
  const kpi = (id) => html.match(new RegExp(`data-kpi="${id}"[\\s\\S]*?</div></div>`))?.[0] ?? ''
  assert(links.some((l) => l.href === '/inventory/assets?product=git' && l.attrs['data-drill-count'] === '19'))
  assert(links.some((l) => l.href === '/inventory/assets?product=git&product_version=2.47.0' && l.attrs['data-drill-count'] === '5'))
  assert(links.some((l) => l.href === '/inventory/software/git' && l.attrs['data-drill-count'] === '5' && l.attrs['data-drill'] === 'page'))
  assert(html.includes('data-kpi="behind"') && kpi('behind').includes('—') && !/<a[^>]*>—<\/a>/.test(html), 'behind latest: no link, no number for a product with two version schemes')
  assert(html.includes('versions of different schemes are ranked separately'), 'the behind-latest tile says why it has no number')
  // UP-17: nothing on the product page promises Linux packages (no Linux collector ships), whatever schemes the data holds.
  assert(!/Linux packages|Windows and Linux|Ubuntu package version/.test(strip(html)), 'no Linux package claim')
  assert(strip(html).includes('Versions that follow different numbering schemes are ranked apart'), 'the Versions tab note is about schemes')
  // Tabs: Radix Tabs bound to ?tab=, the Versions tab by default.
  for (const t of ['Versions', 'Installed on', 'Timeline', 'Evidence']) assert(html.includes(`>${t}</button>`), t)
  assert(/role="tab" aria-selected="true"[^>]*data-state="active"[^>]*>Versions<\/button>/.test(html), 'the Versions tab is the selected one')
  assert(/role="tab" aria-selected="true"[^>]*>Evidence<\/button>/.test(productPage('/inventory/software/git?tab=evidence')), '?tab= selects the tab')
  // The Versions tab: a bar per version, newest first, N / N-1 / Older, each bar one link with its host count.
  assert(html.includes('Windows and other installers') && html.includes('Debian and Ubuntu packages'))
  assert.deepEqual([...html.matchAll(/inline-flex shrink-0 items-center rounded-full border px-1\.5 py-px text-xs font-semibold leading-4[^"]*"[^>]*>([^<]+)</g)].map((x) => x[1]), ['N', 'N-1', 'Older', 'N', 'N-1'])
  for (const v of PRODUCT.versions) {
    assert(links.some((l) => l.href === `/inventory/assets?product=git&product_version=${encodeURIComponent(v.version)}` && l.attrs['data-drill-count'] === String(v.hosts) && l.attrs['aria-label'].includes(`${v.hosts} ${v.hosts === 1 ? 'host' : 'hosts'}`)), v.version)
  }
  assert(/<div role="group" aria-label="Versions view"[^>]*><button type="button" aria-pressed="true"[^>]*>(?:<svg[\s\S]*?<\/svg>)Chart<\/button><button type="button" aria-pressed="false"/.test(html), 'the chart / table toggle: Chart is pressed')
  assert(html.includes('are ranked apart'), 'the mixed-scheme explanation')
  // A scoped caller sees the scoped response: two versions, one per scheme.
  const scoped = productPage('/inventory/software/git', { product: PRODUCT_SCOPED })
  assert(scoped.includes('data-kpi="hosts"') && anchors(scoped).some((l) => l.href === '/inventory/assets?product=git' && l.attrs['data-drill-count'] === '4'))
}
{
  // Evidence: the raw names, read-only, with a plain-words note; counts are text.
  const html = productPage('/inventory/software/git?tab=evidence')
  assert(html.includes('Raw names grouped into Git') && html.includes('The Git Development Community') && html.includes('Ubuntu Developers'))
  assert(html.includes('<th scope="row"') && html.includes('This table is read-only') && html.includes('cannot be edited in this release') && !/later phase/.test(html))
  assert(html.includes('Rule git'), 'which rule matched')
  // NF-4: at phone width the table scrolls sideways; the region is focusable, named and says it scrolls.
  assert(/<div tabindex="0" role="region" aria-label="Raw names grouped into Git"/.test(html), 'NF-4: the scrolling region is reachable by keyboard')
  assert.deepEqual(html.match(/<h[1-6]/g), ['<h1', '<h2'], 'the evidence tab is one h2 under the product h1 (no heading skipped)')
  const ev = html.slice(html.indexOf('data-evidence-table'))
  assert(!/<a\b/.test(ev.slice(0, ev.indexOf('</table>'))), 'the evidence counts have no list: no links in the table')
  // Installed on: the version filter, the search, the table with the flyout links.
  const inst = productPage('/inventory/software/git?tab=installed&version=1%3A2.34.1-1ubuntu1.11', { installs: { ...INSTALLS, version: '1:2.34.1-1ubuntu1.11', total: 3 } })
  assert(inst.includes('aria-label="Version: 1:2.34.1-1ubuntu1.11"') && inst.includes('aria-label="Search installations"'))
  assert(inst.includes('3 installations with version 1:2.34.1-1ubuntu1.11 (of 19)'))
  assert(anchors(inst).some((l) => l.href === `/inventory/assets?asset=${INSTALLS.items[0].asset_key}`), 'a server opens in the asset flyout')
  assert(!inst.includes('aria-label="Export CSV"'))
  assert(productPage('/inventory/software/git?tab=installed', { permissions: [...VIEW, 'inventory.export'], installs: INSTALLS }).includes('aria-label="Export CSV"'))
  assert(productPage('/inventory/software/git?tab=installed', { installs: INSTALLS }).includes('aria-sort="ascending"'))
  // Timeline: skeleton until the feed arrives; the window selector is a group of pressed buttons.
  const tl = productPage('/inventory/software/git?tab=timeline&days=30')
  assert(tl.includes('aria-label="Time window"') && tl.includes('aria-label="Loading changes"') && /aria-pressed="true"[^>]*>30 days</.test(tl))
  // NF-3: the day groups are h3, so the outline needs its h2 (it went from h1 straight to h3).
  assert.deepEqual(tl.match(/<h[1-6]/g), ['<h1', '<h2'], 'NF-3: the Timeline tab has an h2 under the product h1')
  assert(tl.includes('<h2 class="sr-only">Changes in the last 30 days</h2>'))
  // 404: one designed state (the API gives one body for unknown and out of scope).
  const nf = productPage('/inventory/software/git', { fail: Object.assign(new Error('x'), { response: { status: 404, data: { detail: 'Product not found' } } }) })
  assert(nf.includes('Product not found') && nf.includes('data-state="not-found"') && nf.includes('Back to the software catalog') && nf.includes('tag scope'))
  const err = productPage('/inventory/software/git', { fail: Object.assign(new Error('x'), { response: { status: 500, data: {} } }) })
  assert(err.includes('Could not load this product') && !err.includes('Product not found'))
  // Loading.
  setUser(USER(VIEW))
  assert(renderAt(newClient(), '/inventory/software/git', 'inventory/software/:productKey', h(m.InventoryProductPage)).includes('aria-label="Loading product"'))
}
{
  // The version table twin and the versions bar of a catalog row, rendered directly.
  const html = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.ProductVersionsTab, { productKey: 'git', data: { ...PRODUCT, unversioned: { hosts: 2, installs: 3, address: '__none__' } } })))
  assert(html.includes('data-unversioned') && html.includes('No version reported') && html.includes('report no version'))
  assert(anchors(html).some((l) => l.href === '/inventory/software/git?tab=installed&version=__none__' && l.attrs['data-drill-count'] === '3'), 'the unversioned installs link to their list (__none__)')
  const only = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.ProductVersionsTab, { productKey: 'x', data: { header: { ...PRODUCT.header, mixed_schemes: false }, versions: [], unversioned: { hosts: 0, installs: 0 } } })))
  assert(only.includes('No installations in view'))
  const bar = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.SoftwareVersionBar, { productKey: 'google-chrome', bar: { top: [{ version: '152.0.8', hosts: 20, scheme: 'semverish', rank: 1 }], other: { versions: 6, hosts: 11 } }, label: 'Chrome: 7 versions' })))
  assert(bar.includes('aria-label="Chrome: 7 versions"') && bar.includes('role="img"') && bar.includes('6 other versions: 11 hosts across them'))
  assert.equal(anchors(bar).length, 1, 'the bucket of several versions is not a link')
}

// The settings form.
function settingsEditor(canManage, { permissions = ['inventory.view', 'inventory.manage'], settings = SETTINGS } = {}) {
  setUser(USER(permissions))
  const qc = newClient()
  qc.setQueryData(API.softwareKeys.settings, settings)
  return m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.SettingsEditor, { canManage }))))
}
{
  const html = settingsEditor(true)
  for (const label of ['Servers are fresh for', 'Network devices are fresh for', 'Stale after', 'Keep change history for', 'Daily snapshot at', 'Show trends on the Overview']) assert(html.includes(label), label)
  // Every number has its limits as help text.
  for (const help of ['1 to 720 hours. Default 24.', '1 to 720 hours. Default 2.', '1 to 365 days. Default 7.', '30 to 3,650 days. Default 400.', "0 to 23 o&#x27;clock, appliance time. Default 0."]) assert(html.includes(help), help)
  assert((html.match(/inputMode="numeric"|inputmode="numeric"/gi) ?? []).length === 5, 'five numeric inputs')
  for (const [field, value] of [['fresh_server', '24'], ['fresh_device', '2'], ['stale_days', '7'], ['change_retention_days', '400'], ['snapshot_hour', '0']]) {
    assert(new RegExp(`data-field="${field}"[\\s\\S]*?value="${value}"`).test(html), `${field} = ${value}`)
  }
  // Every category is a checkbox; the four hidden by default (update, driver, OS component, runtime) are checked.
  // (The switch of Radix renders a hidden checkbox of its own inside a form; the category boxes are the ones with the accent class.)
  const boxes = [...html.matchAll(/<input[^>]*accent-\[rgb\(var\(--primary\)\)\][^>]*>/g)].map((x) => x[0])
  assert.equal(boxes.length, SETTINGS.categories.length)
  assert.equal(boxes.filter((b) => /checked=""/.test(b)).length, 4)
  assert(html.includes('Hidden by default: Update, Driver, OS component, Runtime'))
  assert(strip(html).includes('Switching off "Hide updates, drivers, OS components and runtimes" in the catalog lists them'), 'UI-03: the sentence quotes the switch built from the same list')
  assert(html.includes('Endpoint protection'), 'the new category (X1) is a checkbox of the vocabulary')
  // The form is clean: Save is disabled, the status says so, the audit note is there.
  assert(/<button[^>]*type="submit" disabled="">Save changes<\/button>/.test(html), 'nothing changed: Save is disabled')
  assert(html.includes('All changes saved') && html.includes('Changes to these settings are recorded in the audit log.'))
  assert(html.includes('Restore defaults') && html.includes('Discard'))
  assert(html.includes('A server is Fresh for 24 h after its last inventory'))
  // The effect of the retention setting is explained where it is set.
  assert(html.includes('Sparklines appear once three days of snapshots exist'))
  // Read-only for someone who may view but not manage: every control disabled, nothing to save.
  const ro = settingsEditor(false, { permissions: VIEW })
  assert(ro.includes('data-read-only') && /<fieldset[^>]*disabled/.test(ro))
  assert(!ro.includes('Save changes') && !ro.includes('Restore defaults') && ro.includes('Changes to these settings are recorded in the audit log.'))
  assert(ro.includes('value="24"'), 'the values are still shown')
  // FV1-N4: the numbers are the page's content for this role, not a control to dim (the shared Input halves a disabled field's opacity).
  const roInputs = [...ro.matchAll(/<input[^>]*inputMode="numeric"[^>]*>/g)].map((x) => x[0])
  assert(roInputs.length === 5 && roInputs.every((i) => i.includes('disabled:opacity-100') && i.includes('disabled:bg-surface2')), 'FV1-N4: read-only numbers keep full contrast')
  assert(!roInputs.some((i) => /disabled:opacity-50/.test(i)), 'and no 50% opacity is left in the class list')
  // Before the settings arrive: a skeleton in the shape of the form.
  setUser(USER(['inventory.view', 'inventory.manage']))
  const loading = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: newClient() }, h(m.MemoryRouter, null, h(m.SettingsEditor, { canManage: true }))))
  assert(loading.includes('aria-label="Loading settings"'))
  // A failure to load is a failure, not an empty form.
  const qc = newClient()
  failQuery(qc, API.softwareKeys.settings, Object.assign(new Error('x'), { response: { status: 500, data: {} } }))
  const failed = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.SettingsEditor, { canManage: true }))))
  assert(failed.includes('Could not load the inventory settings') && failed.includes('Retry'))
}
{
  // The page around it: the tab for people who manage, a read-only notice for the rest, nothing flashed while permissions load.
  const asPage = (user) => { setUser(user); return renderAt(newClient(), '/inventory/settings', 'inventory/settings', h(m.InventorySettingsPage)) }
  const manager = asPage(USER(['inventory.view', 'inventory.manage']))
  assert(manager.includes('Data &amp; retention') && manager.includes('role="tab"') && !manager.includes('Current settings'))
  const viewer = asPage(USER(VIEW))
  assert(viewer.includes('inventory.manage') && viewer.includes('You can read these settings but not change them') && viewer.includes('Current settings'))
  assert(!viewer.includes('Data &amp; retention'))
  assert(viewer.includes('data-read-only-notice'))
  const unknown = asPage({ id: 'u', username: 'u', email: 'u@x', role: 'custom' })
  assert(unknown.includes('Loading settings'), 'while the permissions load the form waits')
}

/* ═══ Fix round: findings of the Phase 1 review (each assertion names its finding) ════════════ */

// X1 / FE-04 / FE-11 (software side): the vocabulary, exact-match values and the API's own limits.
{
  assert(S.SOFTWARE_CATEGORIES.includes('endpoint-protection'), 'X1: the category exists')
  assert.equal(S.SOFTWARE_CATEGORIES.indexOf('endpoint-protection'), S.SOFTWARE_CATEGORIES.indexOf('security') + 1, 'placed after security, as the API lists it')
  assert.equal(S.categoryLabel('endpoint-protection'), 'Endpoint protection', 'user-visible copy says Endpoint protection, never EDR/AV')
  assert.deepEqual(S.parseCatalogFilters('category=endpoint-protection').filters, { category: ['endpoint-protection'] })
  assert.equal(S.catalogFilterChips({ category: ['endpoint-protection'] })[0].text, 'Endpoint protection')

  // FE-04: an exact value keeps its internal white space (the API compares the stored string); the search is words.
  assert.equal(S.parseCatalogFilters(new URLSearchParams({ publisher: '  Acme   Corp  ' })).filters.publisher, 'Acme   Corp', 'ends trimmed, inside untouched')
  assert.equal(S.writeCatalogFilters({ publisher: 'Acme   Corp' }).get('publisher'), 'Acme   Corp', 'and written back the same')
  assert.equal(S.parseCatalogFilters(new URLSearchParams({ q: '  google    chrome ' })).filters.q, 'google chrome', 'q is words: runs of white space collapse')
  assert.equal(P.readProductState(new URLSearchParams({ tab: 'installed', version: ' 2.0 beta  2 ' })).installed.version, '2.0 beta  2', 'a version keeps its internal spaces')
  assert.equal(P.readProductState(new URLSearchParams({ tab: 'installed', q: ' a   b ' })).installed.q, 'a b')
  assert.equal(P.installedHref('git', '2.0 beta  2'), '/inventory/software/git?tab=installed&version=2.0+beta++2', 'the link carries the version as it is')
  // FE-11: the limits are the API's (publisher 96, version and search 200), so the page shows its notice instead of a 422.
  const long = (n) => 'x'.repeat(n)
  assert.equal(S.parseCatalogFilters(new URLSearchParams({ publisher: long(96) })).filters.publisher, long(96))
  const over = S.parseCatalogFilters(new URLSearchParams({ publisher: long(97) }))
  assert.equal(over.filters.publisher, undefined)
  assert.deepEqual(over.rejected.map((r) => [r.key, r.reason]), [['publisher', 'longer than 96 characters']])
  assert.equal(S.parseCatalogFilters(new URLSearchParams({ q: long(201) })).rejected[0].reason, 'longer than 200 characters')
  assert.equal(P.readProductState(new URLSearchParams({ version: long(201) })).installed.version, undefined)
  assert.equal(P.readProductState(new URLSearchParams({ version: long(200) })).installed.version, long(200))
  assert.equal(S.parseCatalogFilters(new URLSearchParams({ publisher: 'a\u0007b' })).rejected[0].reason, 'contains control characters')
  // productChangesHref has one builder.
  assert.equal(P.productChangesHref('java se', { since: '2026-10-01T12:00:00Z' }), CH.changesPageHref({ subject_key: 'java se', since: '2026-10-01T12:00:00Z' }))
}

// FE-02 / FE-07: the box does not eat keystrokes, and the search lands on the address AS IT IS then.
{
  // The write is a pure plan of the live address: a facet or the switch changed while the text waited survives (FE-07).
  const live = new URLSearchParams('hidden=include&category=browser&sort=name&order=asc')
  const plan = USU.planFilters(live, (current) => USU.patchCatalogFilters(current, { q: 'chrome' }))
  assert.equal(plan.next.toString(), 'q=chrome&category=browser&hidden=include&sort=name&order=asc', 'q joins the filters; the switch, the category and the sort stay')
  assert.equal(plan.push, false, 'typing replaces the history entry')
  assert.equal(USU.planFilters(live, (c) => USU.patchCatalogFilters(c, { category: ['database'] })).push, true, 'a facet is a different view')
  assert.deepEqual(USU.patchCatalogFilters({ q: 'a', hidden: 'include' }, { q: undefined }), { hidden: 'include' }, 'undefined removes a key')
  assert.deepEqual(USU.patchCatalogFilters({ category: ['a'] }, { category: [] }), {}, 'an empty list removes it too')
  assert.equal(USU.planFilters(new URLSearchParams('q=a&page=3'), (c) => USU.patchCatalogFilters(c, { q: 'ab' })).next.get('page'), null, 'a new filter goes back to page 1')
  // The scenario of the review, through the real controller with a fake clock: type, click a facet 100 ms later, the timer fires.
  let now = 0
  let handles = 0
  const timers = new Map()
  const scheduler = {
    setTimeout: (fn, ms) => { const handle = ++handles; timers.set(handle, { at: now + ms, fn }); return handle },
    clearTimeout: (handle) => { timers.delete(handle) },
  }
  const advance = (ms) => {
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
  }
  let address = new URLSearchParams('')
  const ctl = SBM.createSearchController({
    value: '', delay: 300, scheduler,
    onCommit: (q) => { address = USU.planFilters(address, (c) => USU.patchCatalogFilters(c, { q: q || undefined })).next },
  })
  ctl.type('chrome')
  advance(100)
  address = USU.planFilters(address, (c) => USU.patchCatalogFilters(c, { hidden: 'include' })).next   // the click on the switch
  advance(250)
  assert.equal(address.get('q'), 'chrome', 'the search landed')
  assert.equal(address.get('hidden'), 'include', 'FE-07: and the switch was not undone')
  // FE-02: a keystroke just after the commit is not overwritten by the echo of the commit.
  ctl.type('chromes')
  advance(0)
  assert.equal(ctl.getText(), 'chromes', 'typed text stays')
  ctl.syncUrl('chrome')                                 // the router re-renders with the committed q
  assert.equal(ctl.getText(), 'chromes', 'FE-02: the echo of the box\'s own commit does not reset it')
  ctl.syncUrl('')                                       // Back / Clear all from outside
  assert.equal(ctl.getText(), '', 'a change from outside is followed')
}

// UI-03 / DT-08 / X5: the switch is named after the categories the settings hide.
{
  assert.equal(S.hideSwitchLabel(['update', 'driver', 'os-component', 'runtime']), 'Hide updates, drivers, OS components and runtimes')
  assert.equal(S.hideSwitchLabel(['driver', 'os-component', 'runtime', 'update']), 'Hide updates, drivers, OS components and runtimes', 'the API sends them sorted; the label is in the order people say them')
  assert.equal(S.hideSwitchLabel(['runtime']), 'Hide runtimes', 'an administrator who hides only runtimes')
  assert.equal(S.hideSwitchLabel(['update', 'driver', 'os-component']), 'Hide updates, drivers and OS components', 'the old default no longer promises runtimes')
  assert.equal(S.hideSwitchLabel(['browser', 'update']), 'Hide updates and browser', 'a category with no plural word falls back to its label')
  assert.equal(S.hideSwitchLabel([]), 'Hide categories hidden by default', 'neutral fallback when nothing is hidden')
  assert.equal(S.hideSwitchLabel(undefined), 'Hide categories hidden by default', 'and before the catalog has answered')
  assert.equal(S.hideSwitchLabel(['update', 'driver', 'os-component', 'runtime', 'browser']), 'Hide 5 categories hidden by default', 'a long list is counted; the tooltip names it')
  assert.equal(S.hiddenCategoryNames(['driver', 'os-component', 'runtime', 'update']), 'Update, Driver, OS component, Runtime')
  assert.deepEqual(S.orderHiddenCategories(['runtime', 'zzz', 'update', 'browser', 'Update']), ['update', 'runtime', 'browser', 'zzz'])
  // The toolbar renders it, with the list in the tooltip.
  const bar = (hiddenCategories) => m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.SoftwareToolbar, {
    filters: {}, onPatch() {}, chips: [], hrefWith: () => '/', onClear() {}, publishers: [], publisherNote: null, hiddenCategories, canExport: false, onExport() {}, exporting: false, total: 3, loading: false,
  })))
  const withRuntime = bar(['driver', 'os-component', 'runtime', 'update'])
  assert(withRuntime.includes('Hide updates, drivers, OS components and runtimes') && withRuntime.includes('Hides products in: Update, Driver, OS component, Runtime.'))
  assert(bar(['runtime']).includes('>Hide runtimes</label>'))
  assert(bar(undefined).includes('Hide categories hidden by default'))
  const none = bar([])
  assert(none.includes('Hide categories hidden by default') && none.includes('aria-checked="false"') && none.includes('No category is hidden by default'), 'nothing hidden: the switch is disabled and says why')
}

// DT-05 / UP-06: a first-seen at or before the capture start is "before <date>", never a date that reads like an installation day.
{
  const START = '2026-10-09T08:15:00.250000Z'
  const day = S.shortDate(START)
  const before = S.firstSeenView('2026-10-09T08:15:00Z', START)
  assert.equal(before.text, `before ${day}`)
  assert.equal(before.before, true)
  assert.match(before.title, /Already there when Inventory began recording/)
  assert.match(S.firstSeenView('2024-04-22T00:00:00Z', START).title, /earliest date known is 2024-04-22/, 'an install date the machine reported stays in the tooltip')
  const after = S.firstSeenView('2026-10-10T09:00:00Z', START)
  assert.deepEqual([after.text, after.before], [S.shortDate('2026-10-10T09:00:00Z'), false], 'a row first seen after the capture began is a real date')
  assert.deepEqual([S.firstSeenView('2026-10-09T08:15:00.250Z', START).before, S.firstSeenView('2026-10-09T08:15:00.251Z', START).before], [true, false], 'at the capture start is before it; a millisecond later is not')
  assert.equal(S.firstSeenView('2024-01-01T00:00:00Z', null).text, S.shortDate('2024-01-01T00:00:00Z'), 'without a capture start the date is as stored')
  assert.equal(S.firstSeenView('2024-01-01T00:00:00Z', undefined).before, false)
  assert.equal(S.firstSeenView(null, START).text, '—')
  assert.equal(S.firstSeenView('garbage', START).text, '—')
  assert.equal(S.firstSeenText('2026-10-09T08:00:00Z', START), `First seen before ${day}`)
  assert.equal(S.firstSeenText(null, START), 'First seen unknown')
  assert.equal(P.productFactLine({ first_seen: '2026-10-09T08:00:00Z', last_change: null }, S.shortDate, START), `First seen before ${day} · no change recorded yet`)
  assert.equal(P.productFactLine(PRODUCT.header, S.shortDate), 'First seen 2026-07-19 · last change 2026-10-05', 'without a capture start nothing changes')
  assert.equal(HS.historyProbeSince(Date.parse('2026-10-09T10:34:56Z')), '2026-10-09T09:00:00Z', 'a one-hour window rounded down: one request all hour')

  // Rendered: the catalog table, the product header and the Installed on tab.
  const rowBefore = { ...CATALOG.items[0], first_seen: '2026-10-09T08:00:00Z' }
  const rowReal = { ...CATALOG.items[1], first_seen: '2026-10-12T08:00:00Z' }
  const table = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.SoftwareTable, { rows: [rowBefore, rowReal], sort: 'hosts', order: 'desc', onSort() {}, caption: 'x', historyStart: START })))
  assert(strip(table).includes(`before ${day}`) && table.includes('data-first-seen="before"'), 'the catalog says before')
  assert(strip(table).includes(S.shortDate('2026-10-12T08:00:00Z')), 'and a real date stays a date')
  const header = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.ProductHeader, { data: { ...PRODUCT, header: { ...PRODUCT.header, first_seen: '2026-10-09T08:00:00Z' } }, historyStart: START })))
  assert(strip(header).includes(`First seen before ${day}`) && header.includes('Already there when Inventory began recording'))
  function installedWith(historyStart) {
    setUser(USER(VIEW))
    const qc = newClient()
    const q = P.readProductState(new URL('/inventory/software/git?tab=installed', 'http://x').searchParams).installed
    qc.setQueryData(API.softwareKeys.installations('git', JSON.stringify({ version: q.version, q: q.q, sort: q.sort, order: q.order, page: q.page, page_size: q.pageSize })), { ...INSTALLS, items: INSTALLS.items.map((r) => ({ ...r, first_seen_at: '2026-10-09T08:00:00Z' })) })
    qc.setQueryData(API.softwareKeys.product('git'), PRODUCT)
    qc.setQueryData(HS.HISTORY_START_KEY, historyStart)
    return renderAt(qc, '/inventory/software/git?tab=installed', 'inventory/software/:productKey', h(m.InventoryProductPage))
  }
  const withStart = installedWith(START)
  assert(strip(withStart).includes(`before ${day}`), 'the Installed on tab says before, from the capture start the page read')
  assert(!strip(installedWith(null)).includes(`before ${day}`), 'no capture start known: the stored date is shown')
}

// UI-15 / UI-19: numbers that are links have names; copy that read wrong now agrees in number and speaks words.
{
  const row = CATALOG.items[0]
  const vr = P.versionRows('git', PRODUCT.versions)
  assert(vr.length > 0 && vr.every((r) => /[A-Za-z]/.test(r.hostsLabel) && /[A-Za-z]/.test(r.installsLabel)), 'UI-15: the version table\'s number links are named')
  assert.equal(vr[0].hostsLabel, `${PRODUCT.versions[0].version}: ${PRODUCT.versions[0].hosts} ${PRODUCT.versions[0].hosts === 1 ? 'host' : 'hosts'}, open the list`)
  assert.equal(S.hostsLinkLabel({ name: 'Google Chrome', hosts: 41 }), 'Google Chrome: 41 servers, open the list')
  assert.equal(S.hostsLinkLabel({ name: 'Git', hosts: 1 }), 'Git: 1 server, open the list')
  assert.equal(S.installsLinkLabel({ name: 'Git', installs: 1 }), 'Git: 1 installation, open the list')
  const html = catalogPage('/inventory/software')
  const links = anchors(html)
  for (const r of CATALOG.items.slice(0, 5)) {
    assert(links.some((l) => l.href === `/inventory/assets?product=${encodeURIComponent(r.product_key)}` && l.attrs['aria-label'] === S.hostsLinkLabel(r)), `${r.product_key}: the Hosts number is named`)
    assert(links.some((l) => l.href === `/inventory/software/${encodeURIComponent(r.product_key)}?tab=installed` && l.attrs['aria-label'] === S.installsLinkLabel(r)), `${r.product_key}: the Installs number is named`)
  }
  // UI-15: a link whose visible text is only a number has an accessible name that says what it counts.
  const bare = [...html.matchAll(/<a\s([^>]*)>([^<]*)<\/a>/g)].filter((a) => /^[\d.,\s]+$/.test(a[2]) && a[2].trim() !== '')
  assert(bare.length >= CATALOG.items.length * 2, 'the Hosts and Installs numbers are such links')
  assert(bare.every((a) => /aria-label="[^"]*[A-Za-z][^"]*"/.test(a[1])), 'UI-15: every number link is named')
  // The settings preview and the toast agree in number.
  assert.match(T.freshnessPreview({ fresh_hours: { server: 24, device: 2 }, stale_days: 1 }), /Stale after 1 day\./)
  assert.match(T.freshnessPreview({ fresh_hours: { server: 24, device: 2 }, stale_days: 7 }), /Stale after 7 days\./)
  assert.match(T.saveToast({ changed: ['stale_days'], adjusted: [], settings: { ...SETTINGS.settings, stale_days: 1 } }).description, /older than 1 day as stale/)
  assert.match(T.saveToast({ changed: ['change_retention_days'], adjusted: [], settings: { ...SETTINGS.settings, change_retention_days: 3650 } }).description, /older than 3,650 days is removed/)
  // The Installed on table is spoken with the product's name and the column's label, not the API's identifiers.
  setUser(USER(VIEW))
  const qc = newClient()
  const q = P.readProductState(new URL('/inventory/software/git?tab=installed&sort=install_date&order=desc', 'http://x').searchParams).installed
  qc.setQueryData(API.softwareKeys.installations('git', JSON.stringify({ version: q.version, q: q.q, sort: q.sort, order: q.order, page: q.page, page_size: q.pageSize })), INSTALLS)
  qc.setQueryData(API.softwareKeys.product('git'), PRODUCT)
  const inst = renderAt(qc, '/inventory/software/git?tab=installed&sort=install_date&order=desc', 'inventory/software/:productKey', h(m.InventoryProductPage))
  const caption = inst.match(/<caption class="sr-only">(.*?)<\/caption>/)?.[1] ?? ''
  assert.equal(decode(caption), `Installations of Git: ${INSTALLS.total.toLocaleString('en-US')} in total, sorted by installed, descending`)
  assert(!/install_date|asset_name|git,/.test(caption), 'no key or column id is spoken')
}

// FE-12: a number typed with a leading zero is the number in force, not a change.
{
  const cur = SETTINGS.settings
  const draft = T.draftFromSettings(cur)
  assert.equal(T.parseWhole('024'), 24)
  assert.equal(T.normaliseWhole('024'), '24')
  assert.equal(T.normaliseWhole(' 007 '), '7')
  assert.equal(T.normaliseWhole('0'), '0')
  assert.equal(T.normaliseWhole('12abc'), '12abc', 'what is not a whole number is left as typed')
  assert.equal(T.normaliseWhole(''), '')
  assert.equal(T.draftDirty({ ...draft, fresh_server: '024' }, cur), false, 'FE-12: 024 is 24: nothing to save, so Save stays disabled')
  assert.equal(T.draftDirty({ ...draft, fresh_server: '0024', stale_days: '07' }, cur), false)
  assert.equal(T.draftDirty({ ...draft, fresh_server: '025' }, cur), true)
  assert.equal(T.draftDirty({ ...draft, fresh_server: '' }, cur), true, 'a half-typed value is still a difference')
  assert.equal(T.draftDirty({ ...draft, fresh_server: '2 4' }, cur), true)
  assert.deepEqual(T.normaliseDraft({ ...draft, fresh_server: '024', stale_days: '007', snapshot_hour: 'x' }), { ...draft, fresh_server: '24', stale_days: '7', snapshot_hour: 'x' })
  // And what it sends: the diff of the parsed numbers is empty.
  assert.deepEqual(T.diffSettings(cur, T.validateDraft({ ...draft, fresh_server: '024' }, SETTINGS.limits).values), {})
}

// UI-07: the settings page for a role that may view but not manage (the row, the tab and the route need only inventory.view).
{
  const asPage = (permissions) => { setUser(USER(permissions)); return renderAt(newClient(), '/inventory/settings', 'inventory/settings', h(m.InventorySettingsPage)) }
  // The page: no tab strip, a notice that names the permission (the form itself is a lazy chunk, so the page shows its skeleton here).
  const viewer = asPage(VIEW)
  assert(viewer.includes('data-read-only-notice') && viewer.includes('Current settings') && viewer.includes('inventory.manage'))
  assert(!viewer.includes('role="tab"'), 'no Data & retention tab for a role that cannot change anything')
  const manager = asPage(['inventory.view', 'inventory.manage'])
  assert(manager.includes('role="tab"') && !manager.includes('data-read-only-notice'))
  assert(manager.includes('<h2 class="sr-only">Data &amp; retention</h2>'), 'UI-22: the manage branch has an h2 above the editor\'s h3 sections')
  // The form for that role: the same values, every control disabled, nothing to save.
  const ro = settingsEditor(false, { permissions: VIEW })
  assert(ro.includes('data-read-only') && /<fieldset[^>]*disabled/.test(ro), 'every control is disabled')
  assert(!ro.includes('Save changes') && !ro.includes('Restore defaults') && !ro.includes('Discard'), 'nothing to save')
  assert(strip(ro).includes('Hidden by default: Update, Driver, OS component, Runtime'), 'the values are shown')
  assert(/value="24"/.test(ro) && ro.includes('Endpoint protection'))
  const boxes = ro.match(/<input[^>]*accent-\[rgb[^>]*>/g) ?? []
  assert.equal(boxes.length, SETTINGS.categories.length)
  assert(boxes.every((b) => b.includes('disabled=""')), 'every category box is disabled')
  // The source of truth for who reaches the page: the navigation row needs only inventory.view.
  const row = NAV.NAV_GROUPS.find((g) => g.id === 'inventory').items.find((i) => i.to === '/inventory/settings')
  assert.equal(row.permission, 'inventory.view')
}

// UI-23: the denied screen names the module for a page of the Inventory module, the page elsewhere.
{
  const trail = (url) => { const u = new URL(url, 'http://x'); return NAV.trailForLocation(u.pathname, u.searchParams) }
  assert.equal(DW.deniedPageName(trail('/inventory')), 'Inventory & Compliance', 'not "Overview", which is also the name of the home group')
  assert.equal(DW.deniedPageName(trail('/inventory/software/google-chrome')), 'Inventory & Compliance')
  assert.equal(DW.deniedPageName(trail('/inventory/settings')), 'Inventory & Compliance')
  assert.equal(DW.deniedPageName(trail('/servers/inventory')), 'All Servers', 'other pages keep their own name')
  assert.equal(DW.deniedPageName(trail('/devices')), 'Devices')
  assert.equal(DW.deniedPageName(null), undefined)
}

// (m): the product page names itself in the header's breadcrumb.
{
  BD.setBreadcrumbDetail('/inventory/software/git', 'Git')
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/git'), 'Git')
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/other'), null, 'a name never shows on another address')
  assert.equal(BD.breadcrumbDetailFor('/inventory/software'), null)
  BD.setBreadcrumbDetail('/inventory/software/other', null)
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/git'), 'Git', 'clearing another address does not clear this one')
  BD.setBreadcrumbDetail('/inventory/software/git', '  ')
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/git'), null, 'a blank name clears')
  BD.setBreadcrumbDetail('/inventory/software/git', ' Git for Windows ')
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/git'), 'Git for Windows', 'trimmed')
  BD.setBreadcrumbDetail('/inventory/software/git', null)
  assert.equal(BD.breadcrumbDetailFor('/inventory/software/git'), null)
  // The page registers it and the breadcrumb reads it (static render: the store is read at render time).
  const src = read('src/components/Layout.tsx')
  assert(src.includes("useBreadcrumbDetailFor(pathname)") && src.includes("detailName ?? 'Detail'"), 'the breadcrumb falls back to Detail only while the name is unknown')
  assert(read('src/pages/inventory/InventoryProductPage.tsx').includes('useBreadcrumbDetail(product.data?.header.name)'))
}

// X2: the data_status of the catalog responses is typed, and the product's no-version address is the API's.
{
  for (const r of [CATALOG, CATALOG_SCOPED, PUBLISHERS, PRODUCT, PRODUCT_SCOPED]) {
    assert.deepEqual(Object.keys(r.data_status).sort(), ['normalising', 'pending_identity_rows', 'pending_software_rows'])
    assert.equal(typeof r.data_status.normalising, 'boolean')
  }
  assert.equal(PRODUCT.unversioned.address, API.NO_VERSION)
  assert.equal(API.NO_VERSION, '__none__')
}

console.log('inventory software + settings: types, URL state, models and components OK')
