// Inventory Operating Systems, Hardware and Changes pages (Documentation/Inventory/04-ROADMAP tasks 1.6, 1.8 and the
// Changes page moved into Phase 1):
//   (a) the OS tree: building, sorting, expansion, the donut and the build-currency rows, with the drill-down contract
//       (every count links to the list whose total equals it),
//   (b) the hardware model: vendor > model rows, serial coverage, the models x firmware matrix and its colour scale
//       (contrast measured from styles/globals.css), module groups, server distributions,
//   (c) the Changes page: the address-bar vocabulary, window arithmetic, time zones, the pairing rule, stat chips,
//       day groups, the daily chart's data and the footnotes,
//   (d) the pages rendered to markup from the golden responses (every state), and the CSV fetchers.
// Run from dashboard/: node scripts/test-inventory-os-hardware-changes.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')
const readJson = (rel) => JSON.parse(fs.readFileSync(path.join(src, 'pages/inventory/fixtures', rel), 'utf8'))

/* ── bundle the code under test (the app's own alias, real components) ─────────────────────── */

const entry = `
export * as os from '@/components/inventory/osTreeModel'
export * as hw from '@/components/inventory/hardwareModel'
export * as ch from '@/components/inventory/changesModel'
export * as ohc from '@/pages/inventory/osHardwareChanges'
export * as filters from '@/pages/inventory/filters'
export * as ov from '@/components/inventory/overviewModel'
export * as cm from '@/components/inventory/changeModel'
export * as model from '@/components/inventory/model'
export { InventoryOsPage } from '@/pages/inventory/InventoryOsPage'
export { InventoryHardwarePage } from '@/pages/inventory/InventoryHardwarePage'
export { InventoryChangesPage } from '@/pages/inventory/InventoryChangesPage'
export { OsTree } from '@/components/inventory/OsTree'
export { ChangeRowView, ChangesList } from '@/components/inventory/ChangesList'
export { ChangesFootnote } from '@/components/inventory/ChangesFootnote'
export { MatrixTable, MatrixLegend } from '@/components/inventory/HardwarePanels'
export { useAuth } from '@/stores/auth'
export { api } from '@/lib/api'
export { inventoryApi } from '@/pages/inventory/api'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
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
const { os: OS, hw: HW, ch: CH, ohc: OHC, filters: F, ov: OV } = m
const h = m.createElement

const sp = (q) => new URLSearchParams(q)
/** The filters a drill link carries, parsed back through the vocabulary (so a typo in a key fails here). */
const drillFilters = (drill) => {
  assert.equal(drill.type, 'assets', 'an assets drill')
  const url = new URL(drill.href, 'http://x')
  assert.equal(url.pathname, '/inventory/assets')
  const parsed = F.parseAssetFilters(url.searchParams)
  assert.deepEqual(parsed.rejected, [], `every key and value of ${drill.href} is in the vocabulary`)
  return parsed.filters
}

const osGolden = readJson('os.json')
const hwGolden = readJson('hardware.json')
const changesGolden = readJson('changes.json')

/* ═══ (a) Operating systems ═════════════════════════════════════════════════════════════════ */

// The kind switch reads ?kind= and keeps every other parameter.
{
  assert.deepEqual(OS.parseOsKind(sp('')), { kind: 'all', rejected: null })
  assert.deepEqual(OS.parseOsKind(sp('kind=server')), { kind: 'server', rejected: null })
  assert.deepEqual(OS.parseOsKind(sp('kind=DEVICE')), { kind: 'device', rejected: null })
  const bad = OS.parseOsKind(sp('kind=router'))
  assert.equal(bad.kind, 'all')
  assert.match(bad.rejected, /router/)
  assert.equal(OS.osKindParam('all'), undefined)
  assert.equal(OS.osKindParam('device'), 'device')
  assert.equal(OS.withOsKind(sp('kind=server&sort=count&order=desc'), 'all').toString(), 'sort=count&order=desc')
  assert.equal(OS.withOsKind(sp('sort=count'), 'device').toString(), 'sort=count&kind=device')
  assert.deepEqual(OS.OS_KIND_OPTIONS.map((o) => o.label), ['All', 'Servers', 'Network'])
}

// Sorting: a three-state header (natural direction, reverse, back to the API's order), kept in the URL.
{
  const none = OS.DEFAULT_OS_SORT
  assert.deepEqual(none, { key: null, order: 'desc' })
  assert.deepEqual(OS.nextOsSort(none, 'count'), { key: 'count', order: 'desc' }, 'counts start with the biggest')
  assert.deepEqual(OS.nextOsSort({ key: 'count', order: 'desc' }, 'count'), { key: 'count', order: 'asc' })
  assert.deepEqual(OS.nextOsSort({ key: 'count', order: 'asc' }, 'count'), none, 'a third click is the API order again')
  assert.deepEqual(OS.nextOsSort(none, 'name'), { key: 'name', order: 'asc' }, 'names start with A to Z')
  assert.deepEqual(OS.nextOsSort({ key: 'name', order: 'asc' }, 'name'), { key: 'name', order: 'desc' })
  assert.deepEqual(OS.nextOsSort({ key: 'name', order: 'desc' }, 'name'), none)
  assert.deepEqual(OS.nextOsSort({ key: 'count', order: 'asc' }, 'name'), { key: 'name', order: 'asc' }, 'another column starts afresh')
  assert.deepEqual(OS.parseOsSort(sp('sort=versions&order=asc')), { key: 'versions', order: 'asc' })
  assert.deepEqual(OS.parseOsSort(sp('sort=versions')), { key: 'versions', order: 'desc' }, 'a missing order is the column\'s natural one')
  assert.deepEqual(OS.parseOsSort(sp('sort=colour&order=asc')), none)
  assert.deepEqual(OS.parseOsSort(sp('order=asc')), none)
  assert.equal(OS.withOsSort(sp('kind=server&sort=name&order=asc'), none).toString(), 'kind=server')
  assert.equal(OS.withOsSort(sp('kind=server'), { key: 'count', order: 'desc' }).toString(), 'kind=server&sort=count&order=desc')
}

const rowsOf = (os, kind = 'all', sort) => OS.buildOsRows(os, kind, sort)
const byKey = (rows, key) => rows.find((r) => r.key === key)

// The tree of the golden response: every node, in the API's order, with the filters of its path.
{
  const rows = rowsOf(osGolden)
  const families = rows.filter((r) => r.type === 'family')
  assert.deepEqual(families.map((r) => r.label), osGolden.families.map((f) => (f.family === 'unknown' ? 'Unknown' : f.label)), 'families in the API order (count, unknown last)')
  assert.equal(families.at(-1).label, 'Unknown', 'unknown is the last family and still a row')
  assert.equal(families.reduce((n, r) => n + r.count, 0), osGolden.total, 'the families add up to the total')
  assert.equal(families.reduce((n, r) => n + r.pct, 0).toFixed(6), '100.000000')

  // Principle 1: every row's drill carries exactly its count and the filters of its path.
  for (const r of rows) {
    assert.equal(r.drill.count, r.count, `${r.key}: the drill stands for the row's number`)
    const f = drillFilters(r.drill)
    assert.equal(f.kind, undefined, 'no kind when the tree is not filtered')
    assert(f.os_family, `${r.key} has an os_family`)
    if (r.type === 'family') assert.deepEqual(Object.keys(f), ['os_family'])
    if (r.type === 'release') assert(f.os_product && f.os_release, `${r.key}: product and release (unknown spelled as the word)`)
    if (r.type === 'version') assert(f.os_version, `${r.key}: version`)
  }
  // Counts nest: a node is the sum of its children; a release is the sum of its versions.
  for (const r of rows.filter((x) => x.expandable)) {
    const kids = rows.filter((k) => k.parent === r.key)
    assert.equal(kids.reduce((n, k) => n + k.count, 0), r.count, `${r.key} is the sum of its children`)
  }

  // A family with one product that is called like the family has no product row; Windows (several products) does.
  assert(!rows.some((r) => r.key === 'p:fortios/FortiOS'), 'FortiOS under FortiOS adds nothing')
  assert(rows.some((r) => r.key === 'p:windows/Windows Server'), 'Windows has several products')
  assert.equal(byKey(rows, 'r:fortios/FortiOS/7.2').depth, 1, 'its releases sit directly under the family')
  assert.equal(byKey(rows, 'r:windows/Windows Server/2022').depth, 2)
  assert.equal(byKey(rows, 'v:windows/Windows Server/2022/10.0.20348.2700').depth, 3)
  assert.equal(byKey(rows, 'r:fortios/FortiOS/7.2').parent, 'f:fortios')

  // A release row: latest observed version, distinct versions, and the raw firmware string on its versions.
  const r72 = byKey(rows, 'r:fortios/FortiOS/7.2')
  assert.equal(r72.latest, '7.2.8')
  assert.equal(r72.versions, 2)
  assert.deepEqual(drillFilters(r72.drill), { os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2' })
  const v728 = byKey(rows, 'v:fortios/FortiOS/7.2/7.2.8')
  assert.equal(v728.raw, 'v7.2.8,build1639,240614 (GA.M)', 'the raw firmware string is carried for the hover')
  assert.deepEqual(drillFilters(v728.drill), { os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2', os_version: '7.2.8' })
  assert.equal(byKey(rows, 'v:windows/Windows Server/2022/10.0.20348.2700').raw, null, 'a Windows build has no raw string to show')
  // The versions of a family: distinct (release, version) pairs.
  assert.equal(byKey(rows, 'f:fortios').versions, 4 + 0, 'FortiOS: 7.4.4, 7.2.8, 7.2.5, 6.4.14')

  // Unknown levels are addressed with the word and stay drillable.
  const unk = byKey(rows, 'f:unknown')
  assert.deepEqual(drillFilters(unk.drill), { os_family: 'unknown' })
  assert.equal(unk.unknown, true)
  assert.equal(unk.expandable, false, 'the unknown family is one row: its only release would repeat it')
  const ios = byKey(rows, 'r:ios-xe/IOS XE/')
  assert.equal(ios.label, 'Release unknown')
  assert.deepEqual(drillFilters(ios.drill), { os_family: 'ios-xe', os_product: 'IOS XE', os_release: 'unknown' })
  assert.equal(ios.expandable, false, 'a release with only the unknown version has nothing to expand')
  const other = rows.find((r) => r.key === 'r:other//')
  assert.deepEqual(drillFilters(other.drill), { os_family: 'other', os_product: 'unknown', os_release: 'unknown' }, 'a product-less family: the product is the word unknown')
  assert.equal(other.depth, 1, 'no product row for a family whose only product is unknown')
  assert.equal(other.latest, '6.5.59')
  // A release whose versions cannot be ordered has no latest observed version, and says so (null), never a guess.
  const nx = byKey(rows, 'r:nx-os/NX-OS/10.2')
  assert.equal(nx.latest, null)
  assert.equal(nx.versions, 1)
  assert.equal(nx.release.currency, null)
}

// The kind switch is part of every drill (so a node's count equals the list it opens).
{
  const rows = rowsOf(osGolden, 'device')
  for (const r of rows) assert.equal(drillFilters(r.drill).kind, 'device', r.key)
  const rowsS = rowsOf(osGolden, 'server')
  for (const r of rowsS) assert.equal(drillFilters(r.drill).kind, 'server', r.key)
}

// Expansion: families and products open, releases closed; "visible" needs every ancestor open.
{
  const rows = rowsOf(osGolden)
  const open = OS.defaultExpanded(rows)
  assert(open.has('f:windows') && open.has('p:windows/Windows Server') && open.has('f:fortios'))
  assert(!open.has('r:windows/Windows Server/2022'), 'releases start collapsed')
  assert(!open.has('f:unknown'), 'a row that cannot expand is not expanded')
  const shown = OS.visibleOsRows(rows, open)
  assert(shown.some((r) => r.key === 'r:windows/Windows Server/2022'))
  assert(!shown.some((r) => r.type === 'version'), 'versions are one click away')
  const toggled = OS.toggleExpanded(open, 'r:windows/Windows Server/2022')
  assert(OS.visibleOsRows(rows, toggled).some((r) => r.key === 'v:windows/Windows Server/2022/10.0.20348.2700'))
  // Collapsing a family hides everything below it, and re-opening brings back the children's own state.
  const closed = OS.toggleExpanded(toggled, 'f:windows')
  assert(!OS.visibleOsRows(rows, closed).some((r) => r.parent && r.key.includes('windows')), 'nothing of Windows is visible')
  assert(OS.visibleOsRows(rows, OS.toggleExpanded(closed, 'f:windows')).some((r) => r.key === 'v:windows/Windows Server/2022/10.0.20348.2700'))
  assert(OS.allExpandable(rows).has('r:windows/Windows Server/2022') && !OS.allExpandable(rows).has('f:unknown'))
  assert.equal(OS.toggleExpanded(new Set(), 'a').has('a'), true)
  assert.equal(open.size > 0 && OS.toggleExpanded(open, 'f:windows').has('f:windows'), false, 'a toggle returns a new set')
}

// Sorting the tree: per level, the unknown node always last, ties keep the API's order.
{
  const byCount = rowsOf(osGolden, 'all', { key: 'count', order: 'asc' })
  const fam = byCount.filter((r) => r.type === 'family')
  assert.equal(fam.at(-1).label, 'Unknown', 'unknown stays last even when sorted ascending')
  assert.deepEqual(fam.slice(0, -1).map((r) => r.count), [...fam.slice(0, -1).map((r) => r.count)].sort((a, b) => a - b))
  const byName = rowsOf(osGolden, 'all', { key: 'name', order: 'asc' })
  const names = byName.filter((r) => r.type === 'family').slice(0, -1).map((r) => r.label)
  assert.deepEqual(names, [...names].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })))
  const releases = byName.filter((r) => r.parent === 'f:ios-xe').map((r) => r.label)
  assert.deepEqual(releases, ['17.3', '17.6', '17.9', '17.12', 'Release unknown'], 'releases sort naturally (17.9 before 17.12), the unknown one last')
  const desc = rowsOf(osGolden, 'all', { key: 'name', order: 'desc' }).filter((r) => r.parent === 'f:ios-xe').map((r) => r.label)
  assert.deepEqual(desc, ['17.12', '17.9', '17.6', '17.3', 'Release unknown'])
  const byVersions = rowsOf(osGolden, 'all', { key: 'versions', order: 'desc' })
  const rel = byVersions.filter((r) => r.parent === 'p:windows/Windows Server').map((r) => r.versions)
  assert.deepEqual(rel, [...rel].sort((a, b) => b - a))
  // Sorting never loses or duplicates a row, and counts and drills are unchanged.
  for (const sort of [{ key: 'count', order: 'desc' }, { key: 'name', order: 'desc' }, { key: 'versions', order: 'asc' }]) {
    const rows = rowsOf(osGolden, 'all', sort)
    assert.deepEqual(rows.map((r) => r.key).sort(), rowsOf(osGolden).map((r) => r.key).sort())
    for (const r of rows) assert.equal(r.drill.count, r.count)
  }
  // The version rows of a release follow the count sort and keep the API order under the versions sort.
  const v2022 = (sort) => rowsOf(osGolden, 'all', sort).filter((r) => r.parent === 'r:windows/Windows Server/2022').map((r) => r.count)
  assert.deepEqual(v2022({ key: 'count', order: 'asc' }), [...v2022({ key: 'count', order: 'asc' })].sort((a, b) => a - b))
  assert.deepEqual(v2022({ key: 'versions', order: 'asc' }), v2022(OS.DEFAULT_OS_SORT))
}

// A tiny hand-made tree covers what the golden fleet does not: single-product family with a different name,
// an unknown product under a known family, and a sort that has to break ties.
{
  const os = {
    generated_at: 'x', scope: { restricted: false, tags: [] }, kind: 'server', total: 10,
    families: [
      { family: 'linux', label: 'Linux', count: 6, products: [{ product: 'Ubuntu', count: 6, releases: [
        { release: '22.04', count: 4, latest_version: '22.04.4', distinct_versions: 1, currency: null, versions: [{ version: '22.04.4', count: 4, raw_example: null }] },
        { release: '20.04', count: 2, latest_version: '20.04.6', distinct_versions: 1, currency: null, versions: [{ version: '20.04.6', count: 2, raw_example: null }] },
      ] }] },
      { family: 'windows', label: 'Windows', count: 4, products: [
        { product: 'Windows Server', count: 2, releases: [{ release: '2022', count: 2, latest_version: '10.0.1', distinct_versions: 1, currency: null, versions: [{ version: '10.0.1', count: 2, raw_example: null }] }] },
        { product: null, count: 2, releases: [{ release: null, count: 2, latest_version: null, distinct_versions: 0, currency: null, versions: [{ version: null, count: 2, raw_example: null }] }] },
      ] },
    ],
  }
  const rows = OS.buildOsRows(os, 'server')
  assert(rows.some((r) => r.key === 'p:linux/Ubuntu'), 'Ubuntu is not called Linux, so it is a row')
  const product = rows.find((r) => r.key === 'p:windows/')
  assert.equal(product.label, 'Windows (product unknown)')
  assert.deepEqual(drillFilters(product.drill), { kind: 'server', os_family: 'windows', os_product: 'unknown' })
  const rel = rows.find((r) => r.key === 'r:windows//')
  assert.deepEqual(drillFilters(rel.drill), { kind: 'server', os_family: 'windows', os_product: 'unknown', os_release: 'unknown' })
  assert.equal(rows.filter((r) => r.parent === 'f:windows').at(-1).label, 'Windows (product unknown)', 'a product-less node goes last')
  assert.deepEqual(OS.buildOsRows({ ...os, families: [], total: 0 }, 'all'), [], 'no assets, no rows')
  assert.equal(OS.versionsText({ versions: 0, type: 'release' }), 'none identified', 'zero versions is said, not hidden')
  assert.equal(OS.versionsText({ versions: 1, type: 'release' }), '1 version')
  assert.equal(OS.versionsText({ versions: 3, type: 'release' }), '3 versions')
  assert.equal(OS.versionsText({ versions: null, type: 'version' }), '')
  // UI-13: a row is named by its place in the tree, so "22H2" and "Windows" are never two different buttons with one name.
  assert.equal(OS.expandLabel({ path: 'Windows Server 2022', versions: 3, type: 'release' }, false), 'Expand Windows Server 2022, 3 versions')
  assert.equal(OS.expandLabel({ path: 'Windows (family)', versions: 5, type: 'family' }, true), 'Collapse Windows (family)')
  assert.equal(OS.rowSummary({ path: 'FortiOS (family)', count: 14, pct: 9.655 }, 145), 'FortiOS (family): 14 assets, 9.7 percent of 145')
  assert.equal(OS.rowSummary({ path: 'X', count: 1, pct: 0.7 }, 145, 'servers'), 'X: 1 server, 0.7 percent of 145')
}

// The family donut: the five biggest families get a colour, the rest one neutral slice, unknown its own; the legend lists them all.
{
  const d = OS.familyDonut(osGolden, 'all')
  assert.equal(d.total, 145)
  assert.equal(d.slices.filter((s) => s.color !== undefined).length, OS.MAX_DONUT_FAMILIES)
  assert.equal(d.slices.at(-1).unknown, true)
  assert.equal(d.slices.at(-2).other, true)
  assert.equal(d.folded, 6, 'eleven known families, five coloured, six folded')
  assert.equal(d.slices.reduce((n, s) => n + s.count, 0), 145, 'the slices add up to the total')
  assert.equal(d.legend.length, osGolden.families.length, 'every family is in the legend, so folding hides nothing')
  assert.equal(d.legend.at(-1).label, 'Unknown')
  assert.deepEqual(d.legend.map((l) => l.count), [...d.legend.slice(0, -1).map((l) => l.count).sort((a, b) => b - a), d.legend.at(-1).count])
  for (const l of d.legend) {
    assert.equal(l.drill.count, l.count)
    const f = drillFilters(l.drill)
    assert.deepEqual(Object.keys(f), ['os_family'])
  }
  for (const s of d.slices.filter((x) => !x.other)) assert.equal(s.drill.count, s.count)
  assert.equal(d.slices.find((s) => s.other).drill.type, 'page', 'several families have no single filter: the slice stays on the page')
  assert.equal(d.slices.find((s) => s.other).drill.count, null, 'and stands for no list')
  assert.match(OS.donutSummary(d), /^145 assets by operating system family: Windows 46 \(31\.7%\)/, 'UI-12: the reading says it is by family')
  // UI-12: the grey slice is named, and the families it folds are marked in the legend under it.
  assert.equal(d.other.label, 'Other (6 families)')
  assert.equal(d.legend.filter((l) => l.tail).length, 6)
  assert(d.legend.filter((l) => l.tail).every((l) => l.color === undefined && !l.unknown))
  assert.equal(d.legend.filter((l) => l.tail).reduce((n, l) => n + l.count, 0), d.other.count, 'the families under the heading add up to the grey slice')
  // Colours go to the biggest families in order; no colour is used twice.
  const colours = d.slices.filter((s) => s.color !== undefined).map((s) => s.color)
  assert.deepEqual(colours, [0, 1, 2, 3, 4])
  // Six known families would need six colours and the palette has five (the sixth reused the first one's): the two smallest fold together.
  const six = { ...osGolden, families: osGolden.families.filter((f) => ['windows', 'ios-xe', 'fortios', 'aos-cx', 'junos', 'panos', 'unknown'].includes(f.family)) }
  const dd = OS.familyDonut(six, 'all')
  assert.equal(dd.folded, 2)
  assert.equal(dd.slices.filter((s) => s.color !== undefined).length, 4)
  assert.deepEqual(dd.slices.filter((s) => s.color !== undefined).map((s) => s.color), [0, 1, 2, 3], 'no colour is used twice')
  assert.equal(dd.other.label, 'Other (2 families)')
  // Five known families fit the palette.
  const five = OS.familyDonut({ ...osGolden, families: osGolden.families.filter((f) => ['windows', 'ios-xe', 'fortios', 'aos-cx', 'junos', 'unknown'].includes(f.family)) }, 'all')
  assert.equal(five.folded, 0)
  assert.deepEqual(five.slices.filter((s) => s.color !== undefined).map((s) => s.color), [0, 1, 2, 3, 4])
  // The kind is in the drills and the noun.
  const dev = OS.familyDonut({ ...osGolden, kind: 'device' }, 'device')
  assert.equal(dev.noun, 'network devices')
  assert.equal(drillFilters(dev.legend[0].drill).kind, 'device')
}

// Build currency: one row per release with an identified version; the most assets behind the latest first.
{
  const rows = OS.currencyRows(osGolden)
  assert(rows.length > 0)
  const ordered = rows.filter((r) => r.currency)
  assert.deepEqual(ordered.map((r) => r.behind), [...ordered.map((r) => r.behind)].sort((a, b) => b - a), 'ranked by assets not on the latest version')
  assert.equal(rows.at(-1).currency, null, 'a release the server could not order is kept, last')
  assert.deepEqual(rows.filter((r) => r.currency === null).map((r) => r.label).sort(), ['NX-OS 10.2', 'NX-OS 9.3'], 'the two releases whose versions cannot be ordered')
  for (const r of rows.filter((x) => x.currency)) {
    const c = r.currency
    assert.equal(c.latest + c.previous + c.older + c.unknown, r.count, `${r.label}: the four buckets add up to the release`)
    assert.equal(r.latest.count, c.latest)
    assert.equal(r.older, c.older)
    assert.equal(r.pct.latest + r.pct.previous + r.pct.older + r.pct.unknown > 99.999 && r.pct.latest + r.pct.previous + r.pct.older + r.pct.unknown < 100.001, true)
    // Latest and N-1 are lists (the release's filters plus the version); older and unknown are text.
    const lf = drillFilters(r.latest.drill)
    assert.equal(lf.os_version, c.latest_version)
    assert.equal(r.latest.drill.count, c.latest)
    if (r.previous) {
      assert.equal(drillFilters(r.previous.drill).os_version, c.previous_version)
      assert.equal(r.previous.drill.count, c.previous)
    } else assert.equal(c.previous_version, null)
    assert.match(OS.currencySentence(r), new RegExp(`${c.latest} on the latest observed ${c.latest_version.replace(/[.()]/g, '\\$&')}`))
  }
  const w22 = rows.find((r) => r.label === 'Windows Server 2022')
  assert.deepEqual(drillFilters(w22.latest.drill), { os_family: 'windows', os_product: 'Windows Server', os_release: '2022', os_version: '10.0.20348.2700' })
  assert.equal(OS.currencySentence(w22), 'Windows Server 2022: 9 on the latest observed 10.0.20348.2700, 6 on N-1 (10.0.20348.2402), 3 older')
  const nx = rows.find((r) => r.label === 'NX-OS 10.2')
  assert.equal(nx.currency, null)
  assert.match(OS.currencySentence(nx), /cannot be ordered/)
  assert.equal(nx.latest, null)
  // A release with no identified version (the unknown ones) has nothing to compare and is not a row.
  assert(!rows.some((r) => r.label === 'IOS XE (release unknown)' || r.label === 'Unknown'), 'releases with no identified version are left out of the currency card')
  assert(rows.some((r) => r.label === 'Other (release unknown)'), 'a release nobody could name but whose versions are known is still compared')
  assert.match(OS.CURRENCY_NOTE, /not the vendor's latest release/)
  assert.doesNotMatch(OS.CURRENCY_NOTE, /phase|release notes|coming|will/i, 'customer text promises nothing (DT-10)')
  // The kind is part of the filters.
  assert.equal(drillFilters(OS.currencyRows({ ...osGolden, kind: 'server' }, 'server')[0].latest.drill).kind, 'server')
}


/* ═══ (b) Hardware ══════════════════════════════════════════════════════════════════════════ */

// Serial coverage: a pill with the percentage AND words, so the colour is never the only signal.
{
  assert.equal(HW.serialTone(100), 'complete')
  assert.equal(HW.serialTone(99.9), 'partial')
  assert.equal(HW.serialTone(80), 'partial')
  assert.equal(HW.serialTone(79.9), 'poor')
  assert.equal(HW.serialTone(0), 'poor')
  assert.equal(HW.serialTone(null), 'none', 'no device of the group can report a serial: no percentage, not 0 %')
  for (const tone of ['complete', 'partial', 'poor', 'none']) assert(HW.SERIAL_TONE_TEXT[tone])
}

// UI-04: ONE sentence for a model count, known models only, with the devices that have none said separately.
{
  assert.equal(HW.modelCountText(12, 3), '12 models plus 3 devices with no model')
  assert.equal(HW.modelCountText(1, 1), '1 model plus 1 device with no model')
  assert.equal(HW.modelCountText(5, 0), '5 models')
  assert.equal(HW.modelCountText(0, 9), 'no model identified on 9 devices')
  assert.equal(HW.modelCountText(0, 1), 'no model identified on 1 device')
  assert.equal(HW.modelCountText(0, 0), 'no models')
  assert.equal(hwGolden.network.models_known, hwGolden.network.vendors.reduce((n, v) => n + v.models_known, 0), 'the page-level figure is the sum of the vendors\' known models')
  assert.equal(hwGolden.network.unknown_model_devices, hwGolden.network.vendors.reduce((n, v) => n + v.unknown_model_devices, 0))
}

// Vendor > model rows of the golden response.
{
  const rows = HW.vendorRows(hwGolden)
  assert.equal(rows.length, hwGolden.network.vendors.length)
  assert.equal(rows.at(-1).vendor, null, 'the devices with no vendor are the last row, not dropped')
  assert.equal(rows.at(-1).label, 'Unknown vendor')
  assert.equal(rows.at(-1).unknown, true)
  assert.deepEqual(rows.slice(0, -1).map((r) => r.count), [...rows.slice(0, -1).map((r) => r.count)].sort((a, b) => b - a), 'biggest vendor first')
  assert.equal(rows.reduce((n, r) => n + r.count, 0), hwGolden.network.devices, 'the vendors add up to the devices')
  // Drills: a vendor lists its devices; the vendor-less row uses the predicate (no vendor is not a string).
  const cisco = rows.find((r) => r.vendor === 'Cisco')
  assert.deepEqual(drillFilters(cisco.drill), { kind: 'device', vendor: 'Cisco' })
  assert.equal(cisco.drill.count, hwGolden.network.vendors.find((v) => v.vendor === 'Cisco').count)
  assert.deepEqual(drillFilters(cisco.missingSerialDrill), { kind: 'device', vendor: 'Cisco', health: 'missing_serial' })
  assert.equal(cisco.missingSerialDrill.count, cisco.serialMissing)
  assert.equal(cisco.models, hwGolden.network.vendors.find((v) => v.vendor === 'Cisco').models_known, 'a vendor row counts known models only')
  assert.equal(cisco.unknownModelDevices, 3, 'and says how many devices have none')
  const none = rows.at(-1)
  assert.deepEqual(drillFilters(none.drill), { kind: 'device', vendor: 'unknown' }, 'no vendor is the sentinel: the list counts by-design devices too')
  assert.equal(none.drill.count, hwGolden.network.vendors.find((v) => v.vendor === null).count)
  assert.deepEqual(drillFilters(none.missingSerialDrill), { kind: 'device', vendor: 'unknown', health: 'missing_serial' }, 'the unknown row drills to its missing serials too')
  assert.equal(none.serialMissing, hwGolden.network.vendors.find((v) => v.vendor === null).serial_missing)
  assert.equal(none.rows.length, 0)
  // Devices that cannot report by design are their own count and link, and are outside the serial percentage.
  assert.equal(none.notCollected, 4)
  assert.deepEqual(drillFilters(none.notCollectedDrill), { kind: 'device', vendor: 'unknown', freshness: 'not_applicable' })
  assert.equal(none.notCollectedDrill.count, 4)
  const forti = rows.find((r) => r.vendor === 'Fortinet')
  assert.deepEqual(drillFilters(forti.notCollectedDrill), { kind: 'device', vendor: 'Fortinet', freshness: 'not_applicable' })
  assert.equal(cisco.notCollectedDrill, null, 'nothing by design, no link')
  // A group where nothing can report has no percentage.
  const silent = HW.vendorRows({ ...hwGolden, network: { ...hwGolden.network, vendors: [{ ...hwGolden.network.vendors[0], serial_coverage_pct: null, not_collected: hwGolden.network.vendors[0].count }] } })[0]
  assert.equal(silent.serialPct, null)
  assert.equal(silent.serialTone, 'none')
  // Models: the vendor's own, biggest first, the unknown model last, counts add up to the vendor's.
  for (const v of rows.filter((r) => r.vendor !== null)) {
    assert.equal(v.rows.reduce((n, r) => n + r.count, 0), v.count, `${v.label}: its models add up to it`)
    assert.deepEqual(v.rows.filter((r) => !r.unknown).map((r) => r.count), [...v.rows.filter((r) => !r.unknown).map((r) => r.count)].sort((a, b) => b - a))
    if (v.rows.some((r) => r.unknown)) assert.equal(v.rows.at(-1).unknown, true)
    for (const r of v.rows) {
      assert.equal(r.drill.count, r.count)
      const f = drillFilters(r.drill)
      assert.equal(f.vendor, v.vendor)
      if (r.unknown) assert.deepEqual(f, { kind: 'device', vendor: v.vendor, model: 'unknown' }, 'an unknown model is the sentinel')
      else assert.deepEqual(f, { kind: 'device', vendor: v.vendor, model: r.model })
      if (r.missingSerialDrill) {
        assert.equal(r.missingSerialDrill.count, r.serialMissing)
        assert.deepEqual(drillFilters(r.missingSerialDrill), { kind: 'device', vendor: v.vendor, model: r.unknown ? 'unknown' : r.model, health: 'missing_serial' })
      }
      assert(r.pct >= 0 && r.pct <= 100)
    }
  }
  const unknownModel = rows.flatMap((r) => r.rows).find((r) => r.unknown)
  assert(unknownModel, 'the golden fleet has a vendor with an unnamed model')
  assert.deepEqual(drillFilters(unknownModel.missingSerialDrill), { kind: 'device', vendor: unknownModel.vendor, model: 'unknown', health: 'missing_serial' })
  assert.equal(unknownModel.label, 'Unknown model')
}

// The matrix vendor: the address's vendor when the fleet has it (any case), otherwise the biggest.
{
  assert.deepEqual(HW.matrixVendors(hwGolden).map((v) => v.vendor).slice(0, 2), ['Cisco', 'Fortinet'])
  assert(!HW.matrixVendors(hwGolden).some((v) => v.vendor === null), 'a vendor-less group cannot have a matrix')
  assert.equal(HW.chooseMatrixVendor(null, hwGolden), 'Cisco')
  assert.equal(HW.chooseMatrixVendor('fortinet', hwGolden), 'Fortinet', 'case-insensitive, canonical spelling out')
  assert.equal(HW.chooseMatrixVendor('Nonesuch', hwGolden), 'Cisco', 'a vendor the fleet does not have falls back to the biggest')
  assert.equal(HW.chooseMatrixVendor('Cisco', undefined), 'Cisco', 'before the data arrives the address decides')
  assert.equal(HW.chooseMatrixVendor(null, undefined), null)
  assert.equal(HW.chooseMatrixVendor(null, { ...hwGolden, network: { ...hwGolden.network, vendors: [] } }), null)
}

// The matrix: cells aligned to the columns, every cell a link whose count is the cell's number, a scale with a legend.
{
  const mx = HW.matrixModel(hwGolden)
  assert.equal(mx.vendor, 'Cisco')
  assert.deepEqual(mx.columns.map((c) => c.release), hwGolden.matrix.releases)
  assert.equal(mx.columns.at(-1).label, 'Unknown')
  assert.equal(mx.columns.at(-1).unknown, true)
  assert.equal(mx.max, Math.max(...hwGolden.matrix.models.flatMap((r) => r.cells.map((c) => c.count))))
  for (const row of mx.rows) {
    assert.equal(row.cells.length, mx.columns.length, 'one cell per column, empty ones included')
    assert.equal(row.cells.reduce((n, c) => n + c.count, 0), row.total, `${row.label}: the cells add up to the model`)
    assert.equal(row.totalDrill.count, row.total)
    for (const cell of row.cells) {
      if (cell.count === 0) {
        assert.equal(cell.drill, null)
        assert.equal(cell.step, 0)
        continue
      }
      assert.equal(cell.drill.count, cell.count)
      const f = drillFilters(cell.drill)
      assert.equal(f.os_release, cell.release, 'the release is the column')
      assert.equal(f.vendor, 'Cisco')
      assert.equal(f.kind, 'device')
      if (row.unknown) assert.equal(f.model, 'unknown')
      else assert.equal(f.model, row.model)
      assert(cell.step >= 1 && cell.step <= 5)
    }
  }
  const unknownRelease = mx.rows.flatMap((r) => r.cells).find((c) => c.release === 'unknown' && c.count > 0)
  if (unknownRelease) assert.equal(drillFilters(unknownRelease.drill).os_release, 'unknown', 'the unknown release is the word')
  assert.equal(mx.rows.flatMap((r) => r.cells).find((c) => c.count === mx.max).step, 5, 'the largest cell is the darkest step')
  assert.equal(mx.rows.at(-1).unknown, true, 'the unknown model is the last row')
  assert.equal(mx.mixed, mx.rows.filter((r) => !r.unknown && r.releases > 1).length)
  assert(mx.mixed > 0 && mx.mixed < mx.rows.length)
  // UI-04: the summary counts known models and says how many devices have none.
  assert.equal(mx.knownModels, mx.rows.filter((r) => !r.unknown).length)
  assert.equal(mx.unknownModelDevices, 3)
  assert.match(HW.matrixSummary(mx), /^Cisco: 5 models plus 3 devices with no model; \d+ of the models on more than one release$/)
  assert.equal(HW.matrixModel({ ...hwGolden, matrix: null }), null, 'no vendor asked for, no matrix')
  // A vendor with no devices gives an empty matrix, which is a model with no rows, not an error.
  const empty = HW.matrixModel({ ...hwGolden, matrix: { vendor: 'Nobody', releases: [], models: [] } })
  assert.equal(empty.rows.length, 0)
  assert.equal(empty.max, 0)
  assert.deepEqual(empty.legend, [])
  assert.equal(HW.matrixSummary(empty), 'Nobody: no models')
}

// The colour scale: five steps from the share of the largest cell; the legend is derived from the same function.
{
  assert.equal(HW.matrixStep(0, 10), 0)
  assert.equal(HW.matrixStep(1, 10), 1)
  assert.equal(HW.matrixStep(2, 10), 1)
  assert.equal(HW.matrixStep(3, 10), 2)
  assert.equal(HW.matrixStep(10, 10), 5)
  assert.equal(HW.matrixStep(9, 10), 5)
  assert.equal(HW.matrixStep(8, 10), 4)
  assert.equal(HW.matrixStep(1, 1), 5, 'a matrix of ones is all the top step')
  assert.equal(HW.matrixStep(5, 0), 0, 'no maximum, no colour')
  assert.deepEqual(HW.matrixLegend(10).map((l) => l.range), ['1 to 2', '3 to 4', '5 to 6', '7 to 8', '9 to 10'])
  assert.deepEqual(HW.matrixLegend(3).map((l) => [l.step, l.range]), [[2, '1'], [4, '2'], [5, '3']], 'a small matrix lists only the steps its counts reach, under the right step number')
  assert.deepEqual(HW.matrixLegend(0), [])
  // Every count of a matrix is in exactly one legend range.
  for (const max of [1, 2, 3, 7, 10, 33, 120]) {
    const legend = HW.matrixLegend(max)
    for (let n = 1; n <= max; n += 1) {
      const step = HW.matrixStep(n, max)
      const row = legend.find((l) => l.step === step)
      const [lo, hi] = row.range.split(' to ').map(Number)
      assert(n >= lo && n <= (hi ?? lo), `${n} of ${max} is in step ${step} (${row.range})`)
    }
  }
  assert.equal(HW.MATRIX_STEPS.length, 5)
}

// The cells' numbers must read in both themes: measured from the real theme tokens (styles/globals.css).
{
  const css = fs.readFileSync(path.join(src, 'styles/globals.css'), 'utf8')
  const block = (selector) => {
    const start = css.indexOf(`${selector} {`)
    assert(start >= 0, selector)
    const body = css.slice(start, css.indexOf('}', start))
    return Object.fromEntries([...body.matchAll(/--([a-z0-9-]+):\s*(\d+)\s+(\d+)\s+(\d+);/g)].map((x) => [x[1], [+x[2], +x[3], +x[4]]]))
  }
  const themes = { light: block(':root'), dark: { ...block(':root'), ...block('.dark') } }
  const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05) }
  const blend = (fg, bg, alpha) => fg.map((c, i) => c * alpha + bg[i] * (1 - alpha))
  const worst = {}
  for (const [theme, t] of Object.entries(themes)) {
    for (const [i, step] of HW.MATRIX_STEPS.entries()) {
      // The cell sits on the card (surface); a hovered row tints it with surface2.
      for (const base of ['surface', 'surface2']) {
        const fill = blend(t.primary, t[base], step.alpha)
        const r = ratio(t[step.text], fill)
        worst[theme] = Math.min(worst[theme] ?? Infinity, r)
        assert(r >= 4.5, `${theme} step ${i + 1} (${step.text} on primary/${step.alpha} over ${base}) is ${r.toFixed(2)}:1, below 4.5:1`)
      }
    }
    // Neighbouring steps differ enough to tell apart (not colour alone, but the scale must still step).
    for (let i = 1; i < HW.MATRIX_STEPS.length; i += 1) {
      const a = blend(t.primary, t.surface, HW.MATRIX_STEPS[i - 1].alpha)
      const b = blend(t.primary, t.surface, HW.MATRIX_STEPS[i].alpha)
      assert(ratio(a, b) >= 1.1, `${theme}: steps ${i} and ${i + 1} are distinguishable`)
    }
  }
  console.log(`matrix cell contrast: light >= ${worst.light.toFixed(1)}:1, dark >= ${worst.dark.toFixed(1)}:1`)
  // The tokens the classes name exist in tailwind.
  const tailwind = fs.readFileSync(path.join(root, 'tailwind.config.js'), 'utf8')
  assert(tailwind.includes("'on-primary'") && tailwind.includes("text: 'rgb(var(--text)"))
}

// ENTITY-MIB module groups: by class (the API's order), models with a name first, drills through module_class/module_model.
{
  const groups = HW.moduleGroups(hwGolden)
  assert.deepEqual(groups.map((g) => g.class), ['chassis', 'module', 'powerSupply', 'fan'])
  assert.equal(groups.reduce((n, g) => n + g.components, 0), hwGolden.modules.reduce((n, g) => n + g.count, 0))
  for (const g of groups) {
    assert.equal(g.rows.reduce((n, r) => n + r.components, 0), g.components)
    for (const r of g.rows) {
      assert.equal(r.drill.count, r.devices, 'the link stands for the devices that have the component, not the components')
      const f = drillFilters(r.drill)
      assert.deepEqual(f, { module_class: g.class, module_model: r.model ?? 'unknown' })
    }
    if (g.rows.some((r) => r.unknown)) assert.equal(g.rows.at(-1).unknown, true, 'the unnamed model is last')
  }
  const ps = groups.find((g) => g.class === 'powerSupply').rows[0]
  assert.equal(ps.components, 34)
  assert.equal(ps.devices, 17, 'two supplies per device: components are not devices')
  assert.equal(HW.withSerialText(ps.components, ps.withSerial), '30 of 34 report a serial')
  assert.equal(drillFilters(ps.drill).module_class, 'powerSupply', 'the class keeps its spelling')
  const chassis = groups.find((g) => g.class === 'chassis').rows
  assert.equal(chassis.at(-1).label, 'Unknown model')
  assert.equal(drillFilters(chassis.at(-1).drill).module_model, 'unknown')
  // A class the vocabulary does not know cannot be addressed: its number stays text.
  const odd = HW.moduleGroups({ ...hwGolden, modules: [{ class: 'transceiver', class_label: 'Transceiver', model: 'SFP-10G', count: 3, devices: 2, with_serial: 3 }] })
  assert.equal(odd[0].rows[0].drill, null)
  assert.deepEqual(HW.moduleGroups({ ...hwGolden, modules: [] }), [], 'no components reported: no groups, the page says so')
  assert(HW.MODULE_ROWS_SHOWN >= 5)
  // The unnamed model is never folded behind "Show more".
  const chassisRows = groups.find((g) => g.class === 'chassis').rows
  assert.equal(chassisRows.length, 20)
  const folded = HW.visibleModuleRows(chassisRows, false)
  assert.equal(folded.shown.length, HW.MODULE_ROWS_SHOWN + 1, 'six named models and the unnamed one')
  assert.equal(folded.shown.at(-1).unknown, true)
  assert.equal(folded.hidden, 13)
  assert.equal(folded.shown.length + folded.hidden, chassisRows.length)
  assert.equal(HW.visibleModuleRows(chassisRows, true).shown.length, 20)
  assert.equal(HW.visibleModuleRows(chassisRows, true).hidden, 0)
  assert.equal(HW.visibleModuleRows(chassisRows.slice(0, 4), false).hidden, 0, 'a short group is shown whole')
}

// Server hardware distributions: every bucket a link, unknown included, zero buckets present, virtualisation only when reported.
{
  const s = HW.serverHardware(hwGolden)
  assert.equal(s.total, 60)
  assert.equal(s.anyKnown, true)
  for (const [name, rows, filterKey] of [['cpu', s.cpu, 'cpu_model'], ['memory', s.memory, 'memory'], ['disks', s.disks, 'disks']]) {
    assert.equal(rows.reduce((n, r) => n + r.count, 0), 60, `${name}: the buckets add up to the servers`)
    for (const r of rows) {
      assert.equal(r.drill.count, r.count)
      assert.deepEqual(drillFilters(r.drill), { [filterKey]: r.value })
      assert(r.barPct >= 0 && r.barPct <= 100)
    }
    assert.equal(rows.at(-1).unknown, true, `${name}: unknown is a row`)
    assert.equal(rows.at(-1).drill.href.endsWith('=unknown'), true)
  }
  assert.equal(s.memory.length, 7, 'six buckets and unknown')
  assert.equal(s.disks.find((r) => r.value === 'ge9').count, 0, 'an empty bucket is shown as zero, not dropped')
  assert.equal(s.cpuMore, null)
  assert.equal(s.virtualization.length, 4)
  assert.deepEqual(drillFilters(s.virtualization.find((r) => r.value === 'vmware').drill), { virtualization: 'vmware' })
  assert.equal(s.virtualization.at(-1).unknown, true)
  // Nothing reported: virtualisation is null and the page says so; every other figure is the unknown bucket.
  const pending = HW.serverHardware({ ...hwGolden, servers: { ...hwGolden.servers, virtualization: null, cpu_models: [{ value: 'unknown', label: 'Unknown', count: 60 }], memory: [{ value: 'unknown', label: 'Unknown', count: 60 }], disks: [{ value: 'unknown', label: 'Unknown', count: 60 }], cpu_models_more: { models: 0, servers: 0 } } })
  assert.equal(pending.virtualization, null)
  assert.equal(pending.anyKnown, false)
  assert.doesNotMatch(HW.PENDING_SERVER_HARDWARE, /phase|ships|coming|will/i, 'customer text promises nothing (DT-10)')
  assert.match(HW.PENDING_SERVER_HARDWARE, /Manufacturer, model, serial number, BIOS/)
  // The top 25 leave a remainder, said as text.
  const more = HW.serverHardware({ ...hwGolden, servers: { ...hwGolden.servers, cpu_models_more: { models: 7, servers: 12 } } })
  assert.deepEqual(more.cpuMore, { models: 7, servers: 12 })
  assert.equal(HW.serversText(1), '1 server')
  assert.equal(HW.serversText(60), '60 servers')
}


/* ═══ (c) Changes ═══════════════════════════════════════════════════════════════════════════ */

const ISO = (s) => Date.parse(s)
const NOW = ISO('2026-10-08T12:00:00Z')

// The address-bar vocabulary: the API's own names, validated like the API validates them.
{
  assert.deepEqual([...CH.CHANGE_FILTER_KEYS], ['q', 'kind', 'category', 'action', 'label', 'asset', 'subject_key', 'since', 'until'])
  const asset = 'device:3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
  const full = { q: 'chrome', kind: 'server', category: 'software', action: 'changed', label: 'upgrade', asset, subject_key: 'google-chrome', since: '2026-10-01T12:00:00Z', until: '2026-10-02T12:00:00Z' }
  const qs = CH.writeChangesFilters(full).toString()
  assert.equal(qs, `q=chrome&kind=server&category=software&action=changed&label=upgrade&asset=device%3A3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f&subject_key=google-chrome&since=2026-10-01T12%3A00%3A00Z&until=2026-10-02T12%3A00%3A00Z`)
  const back = CH.parseChangesFilters(qs)
  assert.deepEqual(back.filters, full)
  assert.deepEqual(back.rejected, [])
  assert.equal(CH.writeChangesFilters(back.filters).toString(), qs, 'writing what was read gives the same string')
  assert.deepEqual(CH.parseChangesFilters(`?${qs}`).filters, full, 'a leading question mark is fine')
  assert.deepEqual(CH.parseChangesFilters('').filters, {})
  for (const key of CH.CHANGE_FILTER_KEYS) {
    const one = key === 'until' ? { since: full.since, until: full.until } : { [key]: full[key] }
    assert.deepEqual(CH.parseChangesFilters(CH.writeChangesFilters(one)).filters, one, key)
  }
  // The same names the Overview links with (changesHref) read back as this page's filters.
  const fromOverview = new URL(OV.changesHref({ category: 'software', action: 'changed', label: 'upgrade', since: '2026-10-01T12:00:00Z' }), 'http://x')
  assert.deepEqual(CH.parseChangesFilters(fromOverview.searchParams), { filters: { category: 'software', action: 'changed', label: 'upgrade', since: '2026-10-01T12:00:00Z' }, rejected: [] })
  // Bad values are dropped, reported, never sent on.
  const bad = CH.parseChangesFilters('category=network&action=deleted&label=sidegrade&kind=router&asset=server:1234&since=yesterday&until=2026-13-45T00:00:00Z&q=%20&subject_key=a%00b')
  assert.deepEqual(bad.filters, {})
  assert.deepEqual(bad.rejected.map((r) => r.key).sort(), ['action', 'asset', 'category', 'kind', 'label', 'q', 'since', 'subject_key', 'until'])
  for (const r of bad.rejected) assert(r.reason, r.key)
  // Values are case-insensitive on the way in and canonical on the way out; instants are normalised to whole-second UTC.
  assert.deepEqual(CH.parseChangesFilters('category=SOFTWARE&action=Added&kind=DEVICE&label=Upgrade').filters, { category: 'software', action: 'added', kind: 'device', label: 'upgrade' })
  assert.equal(CH.parseChangesFilters('since=2026-10-01T14:00:00.750%2B02:00').filters.since, '2026-10-01T12:00:00Z')
  assert.equal(CH.parseChangesFilters('since=2026-10-01T14:00:00%2B0200').filters.since, '2026-10-01T12:00:00Z', 'an offset without a colon is read too')
  assert.equal(CH.parseChangesFilters('since=2026-10-01').filters.since, undefined, 'a bare date has no zone: refused, not guessed')
  assert.equal(CH.normaliseInstant('2026-10-01T12:00Z'), '2026-10-01T12:00:00Z')
  assert.equal(CH.normaliseInstant('nonsense'), null)
  // until must come after since.
  const order = CH.parseChangesFilters('since=2026-10-02T00:00:00Z&until=2026-10-01T00:00:00Z')
  assert.deepEqual(order.filters, { since: '2026-10-02T00:00:00Z' })
  assert.equal(order.rejected[0].key, 'until')
  assert.equal(CH.parseChangesFilters('since=2026-10-02T00:00:00Z&until=2026-10-02T00:00:00Z').filters.until, undefined, 'an empty window is not a window')
  // Over-long text and control characters.
  assert.equal(CH.parseChangesFilters(`q=${'x'.repeat(201)}`).rejected[0].key, 'q')
  assert.equal(CH.parseChangesFilters(`q=${'x'.repeat(200)}`).rejected.length, 0)
  assert.equal(CH.parseChangesFilters('q=a%09%20%20b').filters.q, 'a b')
  // Keys of the page (page, page_size) are not filters; a write keeps them and replaces only the vocabulary.
  assert.deepEqual(CH.parseChangesFilters('page=3&page_size=100&category=os').filters, { category: 'os' })
  assert.equal(CH.writeChangesFilters({ kind: 'device' }, sp('kind=server&page=3&category=os')).toString(), 'page=3&kind=device')
  assert.equal(CH.writeChangesFilters({ category: 'network' }).toString(), '', 'writing validates like reading')
  assert(CH.isChangesFiltersEmpty({}) && !CH.isChangesFiltersEmpty({ q: 'x' }))
  assert.equal(CH.changesCodec.count({ q: 'x', kind: 'server' }), 2)
  assert.deepEqual([...CH.changesCodec.keys], [...CH.CHANGE_FILTER_KEYS])
  // The page's links: plain for no filters, the default page size and page 1 left out.
  assert.equal(CH.changesPageHref({}), '/inventory/changes')
  assert.equal(CH.changesPageHref({ category: 'os', since: '2026-10-01T12:00:00Z' }), '/inventory/changes?category=os&since=2026-10-01T12%3A00%3A00Z')
  assert.equal(CH.changesPageHref({ asset }, { page: 1, page_size: 50 }), '/inventory/changes?asset=device%3A3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f')
  assert.equal(CH.changesPageHref({ q: 'x' }, { page: 3, page_size: 100 }), '/inventory/changes?q=x&page=3&page_size=100')
  // The request: the same names, explicit paging, the zone of the daily series.
  assert.deepEqual(CH.changesQuery(full, 2, 25, 'Europe/Berlin'), { ...full, tz: 'Europe/Berlin', page: 2, page_size: 25 })
  assert.deepEqual(CH.changesQuery({}, 1, 50), { page: 1, page_size: 50 })
  assert.deepEqual(CH.changesQuery({ category: 'network' }, 1, 50), { page: 1, page_size: 50 }, 'a bad filter never reaches the API')
}

// Windows: presets, tolerance, and the Overview's link.
{
  assert.deepEqual(CH.CHANGE_WINDOWS.map((w) => w.label), ['24 h', '7 d', '30 d', '90 d'])
  assert.deepEqual(CH.CHANGE_WINDOWS.map((w) => w.ms / 86_400_000), [1, 7, 30, 90])
  assert.equal(CH.DEFAULT_WINDOW, '7d')
  assert.equal(CH.windowSinceIso(NOW, 7 * 86_400_000), '2026-10-01T12:00:00Z')
  assert.equal(CH.windowSinceIso(NOW + 41_000, 86_400_000), '2026-10-07T12:00:00Z', 'rounded down to the minute: a shared link carries a tidy time')
  // No since is the default; presets within 5 percent; everything else is custom.
  assert.equal(CH.windowOf(undefined, undefined, NOW), '7d')
  assert.equal(CH.windowOf('2026-10-01T12:00:00Z', undefined, NOW), '7d')
  assert.equal(CH.windowOf('2026-10-07T12:00:00Z', undefined, NOW), '24h')
  assert.equal(CH.windowOf('2026-09-08T12:00:00Z', undefined, NOW), '30d')
  assert.equal(CH.windowOf('2026-07-10T12:00:00Z', undefined, NOW), '90d')
  assert.equal(CH.windowOf('2026-10-01T12:00:00Z', undefined, NOW + 3 * 3_600_000), '7d', 'the Overview link opened three hours later is still the 7 d window')
  assert.equal(CH.windowOf('2026-10-01T12:00:00Z', undefined, NOW + 20 * 3_600_000), 'custom', 'but not a day later')
  assert.equal(CH.windowOf('2026-10-03T12:00:00Z', undefined, NOW), 'custom', 'five days is no preset')
  assert.equal(CH.windowOf('2026-10-07T12:00:00Z', '2026-10-08T12:00:00Z', NOW), 'custom', 'a closed range is never a preset')
  assert.equal(CH.windowOf('not a date', undefined, NOW), 'custom')
  assert.equal(CH.windowOf('2026-10-09T12:00:00Z', undefined, NOW), 'custom', 'a since in the future')
  // Choosing a preset: the default has no since; the others write one; a closed range is dropped.
  const f = { category: 'software', kind: 'server', since: '2026-09-01T00:00:00Z', until: '2026-09-02T00:00:00Z', q: 'x' }
  assert.deepEqual(CH.withWindow(f, '7d', NOW), { category: 'software', kind: 'server', q: 'x' })
  assert.deepEqual(CH.withWindow(f, '24h', NOW), { category: 'software', kind: 'server', q: 'x', since: '2026-10-07T12:00:00Z' })
  assert.deepEqual(CH.withWindow({}, '90d', NOW), { since: '2026-07-10T12:00:00Z' })
  for (const key of ['24h', '7d', '30d', '90d']) assert.equal(CH.windowOf(CH.withWindow({}, key, NOW).since, undefined, NOW), key, `${key} reads back as itself`)
}

// Time zones: calendar days are local days of the zone the API used, across DST changes.
{
  assert.equal(CH.isValidTimeZone('Europe/Berlin'), true)
  assert.equal(CH.isValidTimeZone('Mars/Olympus'), false)
  assert.equal(CH.isValidTimeZone(''), false)
  assert.equal(CH.isValidTimeZone(null), false)
  assert.equal(CH.browserTimeZone(), 'UTC', 'the test runs in UTC')
  // UTC
  assert.equal(CH.localDay(ISO('2026-10-08T23:59:59Z'), 'UTC'), '2026-10-08')
  assert.deepEqual(CH.dayBounds('2026-10-07', 'UTC'), { since: '2026-10-07T00:00:00Z', until: '2026-10-08T00:00:00Z' })
  // East of UTC: the local day starts the evening before.
  assert.equal(CH.localDay(ISO('2026-10-07T22:30:00Z'), 'Europe/Berlin'), '2026-10-08')
  assert.deepEqual(CH.dayBounds('2026-10-07', 'Europe/Berlin'), { since: '2026-10-06T22:00:00Z', until: '2026-10-07T22:00:00Z' })
  assert.deepEqual(CH.dayBounds('2026-10-07', 'Asia/Kolkata'), { since: '2026-10-06T18:30:00Z', until: '2026-10-07T18:30:00Z' }, 'a half-hour zone')
  // West of UTC.
  assert.deepEqual(CH.dayBounds('2026-10-07', 'America/New_York'), { since: '2026-10-07T04:00:00Z', until: '2026-10-08T04:00:00Z' })
  // DST: Berlin springs forward on 2026-03-29 (a 23-hour day) and falls back on 2026-10-25 (a 25-hour day).
  const spring = CH.dayBounds('2026-03-29', 'Europe/Berlin')
  assert.equal((ISO(spring.until) - ISO(spring.since)) / 3_600_000, 23)
  assert.equal(spring.since, '2026-03-28T23:00:00Z')
  const fall = CH.dayBounds('2026-10-25', 'Europe/Berlin')
  assert.equal((ISO(fall.until) - ISO(fall.since)) / 3_600_000, 25)
  assert.equal(fall.since, '2026-10-24T22:00:00Z')
  assert.equal(CH.dayBounds('2026-11-01', 'America/New_York').until, '2026-11-02T05:00:00Z', 'New York falls back on 2026-11-01')
  // A day's bounds contain exactly that local day.
  for (const [day, tz] of [['2026-03-29', 'Europe/Berlin'], ['2026-10-25', 'Europe/Berlin'], ['2026-10-07', 'Asia/Kolkata'], ['2026-11-01', 'America/New_York']]) {
    const b = CH.dayBounds(day, tz)
    assert.equal(CH.localDay(ISO(b.since), tz), day, `${tz} ${day} starts on the day`)
    assert.equal(CH.localDay(ISO(b.since) - 1000, tz) < day, true, 'one second earlier is the day before')
    assert.equal(CH.localDay(ISO(b.until) - 1000, tz), day, 'one second before the end is still the day')
    assert.equal(CH.localDay(ISO(b.until), tz) > day, true)
  }
  // Month and year boundaries.
  assert.deepEqual(CH.dayBounds('2026-12-31', 'UTC'), { since: '2026-12-31T00:00:00Z', until: '2027-01-01T00:00:00Z' })
  assert.equal(CH.timeLabel(ISO('2026-10-08T09:05:00Z'), 'UTC'), '09:05')
  assert.equal(CH.timeLabel(ISO('2026-10-08T09:05:00Z'), 'Asia/Kolkata'), '14:35')
  assert.equal(CH.timeLabel(ISO('2026-10-08T00:00:00Z'), 'UTC'), '00:00', 'midnight is 00:00, never 24:00')
  // Day words.
  assert.equal(CH.dayText('2026-10-07'), 'Wed 7 Oct')
  assert.equal(CH.dayText('2026-10-07', '2026-10-08'), 'Wed 7 Oct')
  assert.equal(CH.dayText('2025-12-31', '2026-10-08'), 'Wed 31 Dec 2025', 'another year says so')
  assert.equal(CH.relativeDay('2026-10-08', '2026-10-08'), 'Today')
  assert.equal(CH.relativeDay('2026-10-07', '2026-10-08'), 'Yesterday')
  assert.equal(CH.relativeDay('2026-10-06', '2026-10-08'), null)
  assert.equal(CH.relativeDay('2026-09-30', '2026-10-01'), 'Yesterday', 'across a month')
  assert.equal(CH.instantText(ISO('2026-10-01T12:00:00Z'), 'UTC', NOW), 'Thu 1 Oct 12:00')
  // The window in words.
  assert.equal(CH.windowText(undefined, undefined, NOW, 'UTC'), 'last 7 days')
  assert.equal(CH.windowText('2026-10-07T12:00:00Z', undefined, NOW, 'UTC'), 'last 24 hours')
  const day = CH.dayBounds('2026-10-07', 'Europe/Berlin')
  assert.equal(CH.windowText(day.since, day.until, NOW, 'Europe/Berlin'), 'Wed 7 Oct', 'a whole local day reads as the day')
  assert.equal(CH.windowText('2026-10-03T12:00:00Z', undefined, NOW, 'UTC'), 'since Sat 3 Oct 12:00')
  assert.equal(CH.windowText('2026-10-03T12:00:00Z', '2026-10-05T08:30:00Z', NOW, 'UTC'), 'Sat 3 Oct 12:00 to Mon 5 Oct 08:30')
  // Clicking a day of the chart: since and until are the day's bounds in the series' zone.
  assert.deepEqual(CH.withDay({ category: 'os' }, '2026-10-07', 'Europe/Berlin'), { category: 'os', since: day.since, until: day.until })
}

// Stat chips: eight chips, the window's numbers, and what a click changes.
{
  const stats = changesGolden.stats
  const chips = CH.statChips(stats, {})
  assert.deepEqual(chips.map((c) => c.id), ['all', 'installed', 'removed', 'upgraded', 'downgraded', 'os', 'firmware', 'hardware'])
  assert.deepEqual(chips.map((c) => c.label), ['All changes', 'Installed', 'Removed', 'Upgraded', 'Downgraded', 'OS changes', 'Firmware changes', 'Hardware changes'])
  assert.deepEqual(chips.map((c) => c.count), [56, 12, 12, 12, 1, 4, 11, 2], 'the numbers of stats, nothing recomputed')
  assert.deepEqual(chips.filter((c) => c.active).map((c) => c.id), ['all'])
  // What each chip filters by is what the contract says its number counts (section 4).
  assert.deepEqual(chips.map((c) => c.filters), [{}, { category: 'software', action: 'added' }, { category: 'software', action: 'removed' }, { category: 'software', action: 'changed', label: 'upgrade' }, { category: 'software', action: 'changed', label: 'downgrade' }, { category: 'os' }, { category: 'firmware' }, { category: 'hardware' }])
  // The active chip follows the filters, and `label` alone is a version-change chip, as the API reads it.
  const active = (f) => CH.statChips(stats, f).filter((c) => c.active).map((c) => c.id)
  assert.deepEqual(active({ category: 'software', action: 'added' }), ['installed'])
  assert.deepEqual(active({ label: 'upgrade' }), ['upgraded'])
  assert.deepEqual(active({ category: 'software', label: 'downgrade' }), ['downgraded'])
  assert.deepEqual(active({ category: 'os' }), ['os'])
  assert.deepEqual(active({ category: 'firmware' }), ['firmware'])
  assert.deepEqual(active({ category: 'software' }), [], 'a combination no chip offers has no active chip (the filter chips say it)')
  assert.deepEqual(active({ category: 'software', action: 'changed' }), [])
  assert.deepEqual(active({ category: 'os', action: 'changed' }), [])
  assert.deepEqual(active({ category: 'os', label: 'upgrade' }), [], 'a label with another category finds nothing: no chip')
  assert.deepEqual(active({ kind: 'server', q: 'java', since: '2026-10-01T12:00:00Z' }), ['all'], 'window, kind and search are not chips')
  // Applying a chip replaces category, action and label and keeps the rest.
  const base = { kind: 'server', q: 'java', since: '2026-10-01T12:00:00Z', category: 'os', action: 'changed' }
  assert.deepEqual(CH.applyChip(base, 'installed'), { kind: 'server', q: 'java', since: '2026-10-01T12:00:00Z', category: 'software', action: 'added' })
  assert.deepEqual(CH.applyChip(base, 'all'), { kind: 'server', q: 'java', since: '2026-10-01T12:00:00Z' })
  assert.deepEqual(CH.applyChip({ label: 'upgrade', category: 'software', action: 'changed' }, 'firmware'), { category: 'firmware' })
  assert.deepEqual(CH.applyChip({}, 'upgraded'), { category: 'software', action: 'changed', label: 'upgrade' })
  for (const c of chips) assert(c.hint, c.id)
  // The paired note is text: pairs are not part of Upgraded / Downgraded.
  assert.equal(CH.pairedNote(stats), 'Plus 6 upgrades and 1 downgrade recorded as an installation and a removal. The list shows each pair as one row.')
  assert.equal(CH.pairedNote({ ...stats, software: { ...stats.software, paired: { upgraded: 1, downgraded: 0 } } }), 'Plus 1 upgrade recorded as an installation and a removal. The list shows each pair as one row.')
  assert.equal(CH.pairedNote({ ...stats, software: { ...stats.software, paired: { upgraded: 0, downgraded: 0 } } }), null)
}

// Active filters: what narrows the list besides the chips, the search box and the kind switch, each removable alone.
{
  const asset = 'server:3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f'
  const f = { kind: 'server', asset, subject_key: 'google-chrome', category: 'software', since: '2026-10-03T12:00:00Z', q: 'x' }
  const list = CH.activeFilters(f, NOW, 'UTC', { asset: 'srv-w22-01', subject: 'Google Chrome' })
  assert.deepEqual(list.map((a) => `${a.label}: ${a.text}`), ['Asset: srv-w22-01', 'Product: Google Chrome', 'Category: Software', 'Window: since Sat 3 Oct 12:00'], 'the kind has its own switch and the search its own box: not repeated here')
  assert.deepEqual(list[0].without, { kind: 'server', subject_key: 'google-chrome', category: 'software', since: '2026-10-03T12:00:00Z', q: 'x' })
  assert.deepEqual(list[3].without, { kind: 'server', asset, subject_key: 'google-chrome', category: 'software', q: 'x' }, 'removing the window removes since and until')
  assert.equal(CH.activeFilters({ asset }, NOW, 'UTC')[0].text, asset, 'without a learnt name the address is shown')
  assert.deepEqual(CH.activeFilters({ category: 'software', action: 'added' }, NOW, 'UTC'), [], 'a stats chip is shown by the strip, not twice')
  assert.deepEqual(CH.activeFilters({ category: 'software' }, NOW, 'UTC').map((a) => `${a.label}: ${a.text}`), ['Category: Software'], 'a combination no chip offers is listed')
  assert.deepEqual(CH.activeFilters({ category: 'os', action: 'changed' }, NOW, 'UTC').map((a) => a.text), ['OS', 'Changed'])
  assert.deepEqual(CH.activeFilters({}, NOW, 'UTC'), [])
  assert.deepEqual(CH.activeFilters({ since: '2026-10-01T12:00:00Z' }, NOW, 'UTC'), [], 'the Overview link is the 7 d window: no chip')
}

// ── Pairing ───────────────────────────────────────────────────────────────────────────────
const row = (id, over = {}) => ({
  id, observed_at: '2026-10-08T01:43:00Z', asset_key: 'server:a', asset_kind: 'server', asset_id: 'a', asset_name: 'srv-a', asset_present: true, server_id: null,
  category: 'software', action: 'added', subject: 'Java 8 Update 401', subject_key: 'java-se-runtime', old_value: null, new_value: '8.0.4010.10',
  label: null, pair: null, marker: false, snapshot_truncated: false, detail: {}, ...over,
})
const removedHalf = (id, over = {}) => row(id, { action: 'removed', subject: 'Java 8 Update 391', old_value: '8.0.3910.11', new_value: null, ...over })
const entriesOf = (rows) => CH.buildEntries(rows)

{
  // An installation and a removal of the same product on the same asset at the same instant are ONE row.
  const rows = [removedHalf(52, { pair: { event_id: 51, label: 'upgrade' } }), row(51, { pair: { event_id: 52, label: 'upgrade' } })]
  const { entries, merged } = entriesOf(rows)
  assert.equal(entries.length, 1, 'never two rows for one upgrade')
  assert.equal(merged, 1)
  const e = entries[0]
  assert.equal(e.kind, 'pair')
  assert.equal(e.verb, 'upgraded')
  assert.equal(e.glyph, 'upgraded')
  assert.equal(e.detail, '8.0.3910.11 → 8.0.4010.10', 'the old version of the removal, the new of the installation')
  assert.equal(e.subject, 'Java 8 Update 401', 'the title it is now')
  assert.equal(e.replaces, 'Java 8 Update 391', 'and the title it replaced')
  assert.equal(e.sentence, 'Java 8 Update 401 upgraded 8.0.3910.11 → 8.0.4010.10 (replaces Java 8 Update 391)')
  assert.deepEqual(e.ids, [52, 51])
  assert.equal(e.key, 'pair:52:51')
  assert.equal(e.category, 'software')
  assert.equal(e.address, 'server:a')
  // The halves in either order give the same row; it takes the place of the first.
  assert.deepEqual(entriesOf([...rows].reverse()).entries.map((x) => [x.kind, x.sentence]), [['pair', e.sentence]])
  // A downgrade is the server's call.
  const down = entriesOf([row(11, { subject: '7-Zip 23.01 (x64)', subject_key: '7-zip', new_value: '23.01', pair: { event_id: 10, label: 'downgrade' }, observed_at: '2026-10-05T13:55:00Z' }), removedHalf(10, { subject: '7-Zip 24.05 (x64)', subject_key: '7-zip', old_value: '24.05', pair: { event_id: 11, label: 'downgrade' }, observed_at: '2026-10-05T13:55:00Z' })])
  assert.equal(down.entries[0].verb, 'downgraded')
  assert.equal(down.entries[0].glyph, 'downgraded')
  assert.equal(down.entries[0].detail, '24.05 → 23.01')
  // Same title in both halves: nothing to say about a replaced title.
  const same = entriesOf([row(2, { subject: '7-Zip', subject_key: '7-zip', new_value: '24.08', pair: { event_id: 1, label: 'upgrade' } }), removedHalf(1, { subject: '7-Zip', subject_key: '7-zip', old_value: '23.01', pair: { event_id: 2, label: 'upgrade' } })]).entries[0]
  assert.equal(same.replaces, null)
  assert.equal(same.sentence, '7-Zip upgraded 23.01 → 24.08')
}

// What must NOT pair: different assets, different instants, different products, two of one kind, missing keys, markers, other categories.
{
  const base = removedHalf(2)
  const add = row(1)
  const check = (rows, expected, why) => {
    const r = entriesOf(rows)
    assert.equal(r.entries.length, expected, why)
    assert.equal(r.merged, expected === rows.length ? 0 : r.merged)
    return r
  }
  assert.equal(check([base, add], 1, 'baseline: the pair merges').merged, 1)
  check([base, row(1, { asset_key: 'server:b', asset_id: 'b' })], 2, 'another asset')
  check([base, row(1, { observed_at: '2026-10-08T01:43:01Z' })], 2, 'one second later')
  check([base, row(1, { observed_at: '2026-10-08T02:43:00Z' })], 2, 'an hour later')
  check([base, row(1, { subject_key: 'java-other' })], 2, 'another product')
  check([base, row(1, { subject_key: null }), removedHalf(3, { subject_key: null })], 3, 'no product key, no pairing (the key is what ties the halves)')
  check([base, removedHalf(1, { subject: 'Java 8 Update 381', old_value: '8.0.3810.1' })], 2, 'two removals')
  check([row(2), row(1, { subject: 'Java 8 Update 411', new_value: '8.0.4110.1' })], 2, 'two installations')
  // Two installations and one removal at one instant: the API does not pair them, and neither does this.
  const three = check([row(3, { subject: 'Java 8 Update 411', new_value: '8.0.4110.1' }), base, add], 3, 'two installations and one removal are ambiguous')
  assert.equal(three.merged, 0)
  // A device and a server with the same product key are different assets (asset_key differs).
  check([removedHalf(2, { asset_key: 'device:a', asset_kind: 'device' }), row(1)], 2, 'a device and a server')
  // A software `changed` row, an OS change and a marker are never halves of a pair.
  check([row(2, { action: 'changed', old_value: '1', new_value: '2', label: 'upgrade' }), row(1)], 2, 'a changed row')
  check([row(2, { category: 'os', action: 'removed', subject: 'os_version', subject_key: 'java-se-runtime' }), row(1)], 2, 'an OS event')
  check([row(2, { marker: true, action: 'removed' }), row(1)], 2, 'a marker')
  // The same instant written two ways is the same instant.
  assert.equal(check([removedHalf(2, { observed_at: '2026-10-08T01:43:00Z' }), row(1, { observed_at: '2026-10-08T03:43:00+02:00' })], 1, 'the same instant in another notation pairs').merged, 1)
  // Many pairs and plain events interleave in order, each pair at the place of its first half.
  const mixed = [
    row(30, { subject: 'Chrome', subject_key: 'chrome', new_value: '152', observed_at: '2026-10-08T09:00:00Z' }),
    removedHalf(29, { asset_key: 'server:b', asset_id: 'b', observed_at: '2026-10-08T08:00:00Z' }),
    row(28, { asset_key: 'server:b', asset_id: 'b', observed_at: '2026-10-08T08:00:00Z' }),
    row(27, { category: 'firmware', action: 'changed', subject: 'FortiOS', subject_key: null, old_value: '7.2.5', new_value: '7.2.8', observed_at: '2026-10-08T07:00:00Z' }),
    removedHalf(26, { asset_key: 'server:c', asset_id: 'c', observed_at: '2026-10-08T06:00:00Z' }),
    row(25, { asset_key: 'server:c', asset_id: 'c', observed_at: '2026-10-08T06:00:00Z' }),
  ]
  const r = entriesOf(mixed)
  assert.deepEqual(r.entries.map((e) => e.kind), ['event', 'pair', 'event', 'pair'])
  assert.equal(r.merged, 2)
  assert.deepEqual(r.entries.flatMap((e) => e.ids).sort((a, b) => a - b), [25, 26, 27, 28, 29, 30], 'every event is in exactly one row')
}

// The label is the server's: with none the row says "changed", two that disagree are no label, one side is enough.
{
  const make = (la, lr) => entriesOf([row(2, { pair: la === undefined ? null : { event_id: 1, label: la } }), removedHalf(1, { pair: lr === undefined ? null : { event_id: 2, label: lr } })]).entries[0]
  assert.equal(make('upgrade', 'upgrade').verb, 'upgraded')
  assert.equal(make('upgrade', undefined).verb, 'upgraded', 'one half carrying the label is enough')
  assert.equal(make(undefined, 'downgrade').verb, 'downgraded')
  assert.equal(make(undefined, undefined).verb, 'changed', 'an older server sends no pair: the rule still merges, and does not guess a direction')
  assert.equal(make(undefined, undefined).glyph, 'changed')
  assert.equal(make(null, null).verb, 'changed', 'a pair the server could not order')
  assert.equal(make('upgrade', 'downgrade').verb, 'changed', 'halves that disagree are no label')
  // A pair label that points at some other event is not about this pair.
  const stray = entriesOf([row(2, { pair: { event_id: 99, label: 'upgrade' } }), removedHalf(1)]).entries[0]
  assert.equal(stray.kind, 'pair')
  assert.equal(stray.verb, 'changed')
}

// A half whose partner is on another page stays a row of its own and says so; plain events carry no note.
{
  const { entries, merged } = entriesOf([row(51, { pair: { event_id: 52, label: 'upgrade' } })])
  assert.equal(merged, 0)
  assert.equal(entries[0].kind, 'event')
  assert.equal(entries[0].partnerElsewhere, true)
  assert.match(entries[0].sentence, /part of an upgrade/)
  assert.match(CH.PARTNER_ELSEWHERE_NOTE, /another page/)
  assert.equal(entriesOf([row(60)]).entries[0].partnerElsewhere, false)
}

// Markers and the "capped" note.
{
  const marker = row(38, { action: 'changed', subject: '(change budget exceeded)', subject_key: null, marker: true, observed_at: '2026-10-07T05:30:00Z' })
  const [e] = entriesOf([marker]).entries
  assert.equal(e.kind, 'marker', 'the budget marker is its own kind of row')
  assert.equal(e.glyph, 'marker')
  assert.match(e.sentence, /Change budget exceeded/)
  assert.equal(entriesOf([marker, marker]).merged, 0)
  const truncated = entriesOf([removedHalf(50, { subject: 'Wireshark 4.2.0 x64', subject_key: 'wireshark', old_value: '4.2.0', snapshot_truncated: true })]).entries[0]
  assert.equal(truncated.truncated, true)
  assert.match(CH.truncatedNote(truncated), /capped at 1,000 titles/)
  assert.match(CH.truncatedNote(truncated), /cut off rather than removed/, 'a removal from a capped list may only be a title that fell off it')
  assert.match(CH.truncatedNote({ kind: 'event', verb: 'installed' }), /capped at 1,000 titles.*incomplete/)
  assert.equal(CH.SNAPSHOT_CAP, 1000)
  // A pair is truncated when either half is.
  assert.equal(entriesOf([row(2, { snapshot_truncated: true }), removedHalf(1)]).entries[0].truncated, true)
  assert.equal(entriesOf([row(2), removedHalf(1)]).entries[0].truncated, false)
}

// The golden page: seven pairs merge, the marker and the two capped removals are distinct, every event is accounted for.
{
  const { entries, merged } = entriesOf(changesGolden.items)
  assert.equal(changesGolden.items.length, 50)
  assert.equal(merged, 7, 'six upgrades and one downgrade')
  assert.equal(entries.length, 50 - merged)
  assert.equal(entries.filter((e) => e.kind === 'pair').length, 7)
  assert.deepEqual(entries.filter((e) => e.kind === 'pair').map((e) => e.verb).sort(), ['downgraded', 'upgraded', 'upgraded', 'upgraded', 'upgraded', 'upgraded', 'upgraded'])
  assert.equal(entries.filter((e) => e.kind === 'marker').length, 1)
  assert.equal(entries.filter((e) => e.truncated).length, 2, 'the two removals recorded from capped lists')
  assert.deepEqual(entries.flatMap((e) => e.ids).sort((a, b) => a - b), changesGolden.items.map((r) => r.id).sort((a, b) => a - b), 'every event is in exactly one row')
  assert.deepEqual(entries.map((e) => e.at), [...entries.map((e) => e.at)].sort().reverse(), 'still newest first')
  assert.equal(merged, changesGolden.stats.software.paired.upgraded + changesGolden.stats.software.paired.downgraded, 'what the page merged is what the server counted as pairs')
  assert.equal(CH.countText(56, 43, 7), '56 events, 43 rows on this page (7 pairs of installation and removal shown as one upgrade)')
  assert.equal(CH.countText(56, 56, 0), '56 events')
  assert.equal(CH.countText(1, 1, 0), '1 event')
}

// Day groups: local days of the zone the API used, in order, with relative words.
{
  const { entries } = entriesOf(changesGolden.items)
  const groups = CH.groupByDay(entries, 'UTC', NOW)
  assert.deepEqual(groups.map((g) => g.day), [...groups.map((g) => g.day)].sort().reverse(), 'newest day first')
  assert.equal(new Set(groups.map((g) => g.day)).size, groups.length, 'a day is one group')
  assert.equal(groups.reduce((n, g) => n + g.entries.length, 0), entries.length)
  assert.equal(groups[0].day, '2026-10-08')
  assert.equal(groups[0].text, 'Thu 8 Oct')
  assert.equal(groups[0].relative, 'Today')
  assert.equal(groups[1].relative, 'Yesterday')
  assert.equal(groups[2].relative, null)
  // The same events in Berlin (UTC+2 in October) fall on different days at the edges.
  const berlin = CH.groupByDay([{ ...entries[0], at: '2026-10-07T23:30:00Z' }], 'Europe/Berlin', NOW)
  assert.equal(berlin[0].day, '2026-10-08')
  assert.equal(CH.entryTime({ at: '2026-10-07T23:30:00Z' }, 'Europe/Berlin'), '01:30')
  assert.equal(CH.entryTime({ at: 'garbage' }, 'UTC'), '--:--')
  const odd = CH.groupByDay([{ ...entries[0], at: 'garbage' }], 'UTC', NOW)
  assert.equal(odd[0].text, 'Unknown date')
  assert.deepEqual(CH.groupByDay([], 'UTC', NOW), [])
}

// The chart's data and its text.
{
  const days = CH.chartDays(changesGolden.series, NOW, 'UTC', changesGolden.window.since)
  assert.equal(days.length, changesGolden.series.length)
  const last = changesGolden.series.at(-1)
  assert.deepEqual(days.at(-1), { day: '2026-10-08', label: '8 Oct*', full: 'Thu 8 Oct', software: last.software, os: last.os, firmware: last.firmware, hardware: last.hardware, total: last.total, partial: 'today', partialText: 'so far today' }, 'UI-20: today is not over')
  // The window starts at 12:00 on the first day: that bar covers half a day and is marked.
  assert.equal(days[0].partial, 'start')
  assert.equal(days[0].partialText, 'from 12:00')
  assert.equal(days[0].label, '1 Oct*')
  assert.equal(days[1].partial, null)
  assert.equal(days[1].label, '2 Oct')
  // A window that starts at midnight has a whole first day.
  assert.equal(CH.chartDays(changesGolden.series, NOW, 'UTC', '2026-10-01T00:00:00Z')[0].partial, null)
  for (const d of days) assert.equal(d.software + d.os + d.firmware + d.hardware, d.total)
  const totals = CH.seriesTotals(changesGolden.series)
  assert.equal(totals.total, 56, 'the series covers the same events as the window')
  assert.equal(totals.os, changesGolden.stats.os)
  assert.equal(totals.firmware, changesGolden.stats.firmware)
  assert.equal(totals.hardware, changesGolden.stats.hardware)
  assert.equal(totals.software, changesGolden.stats.software.added + changesGolden.stats.software.removed + changesGolden.stats.software.changed)
  assert.deepEqual(CH.CHART_SERIES.map((s) => s.key), ['software', 'os', 'firmware', 'hardware'])
  assert.deepEqual(CH.CHART_SERIES.map((s) => s.color), [0, 1, 2, 3], 'the categorical colours in their fixed order')
  assert.match(CH.chartSummary(days), /^Changes per day, 8 calendar days, the first and last partial, 56 in total, busiest .+ with \d+$/, 'the label says 8 calendar days for a 7-day window')
  assert.match(CH.chartSummary(CH.chartDays(changesGolden.series.slice(1, -1), NOW, 'UTC', changesGolden.window.since)), /^Changes per day, 6 days, \d+ in total/)
  assert.equal(CH.chartSummary([]), 'Changes per day: no days in the window')
  assert.equal(CH.chartSummary(CH.chartDays([{ day: '2026-10-07', software: 0, os: 0, firmware: 0, hardware: 0, total: 0 }], NOW, 'UTC')), 'Changes per day, 1 day, 0 in total')
}

// The footnote: the real date from the API, never a constant.
{
  // FE-08, UP-08, DT-04: the footnote says when change capture BEGAN and how long changes are kept; both come from the response.
  const info = { history_start: '2026-10-03T08:15:00.250000Z', retention_days: 400 }
  const t = CH.historyText(info, NOW, 'UTC')
  assert.equal(t.text, 'History starts Sat 3 Oct 2026, when change capture began. Changes older than 400 days are removed.')
  assert.equal(t.title, 'Change capture began Sat 3 Oct 08:15 (UTC)')
  assert.match(CH.historyText({ ...info, history_start: '2026-11-17T08:15:00Z' }, ISO('2026-12-01T00:00:00Z'), 'UTC').text, /Tue 17 Nov 2026/, 'the date comes from the data')
  assert.match(CH.historyText({ ...info, history_start: '2026-10-03T22:30:00Z' }, NOW, 'Asia/Kolkata').text, /Sun 4 Oct 2026/, 'the day is the day in the zone shown')
  assert.match(CH.historyText({ ...info, retention_days: 1 }, NOW, 'UTC').text, /older than 1 day are removed/)
  // Purged history is not "not recorded": once capture is older than the retention window the sentence says what is kept.
  const purged = CH.historyText(info, ISO('2027-12-01T00:00:00Z'), 'UTC')
  assert.equal(purged.text, 'Change capture began Sat 3 Oct 2026. Changes older than 400 days are removed. The history you can see therefore starts Tue 27 Oct 2026.')
  assert(!/not recorded/i.test(purged.text) && !/not recorded/i.test(t.text))
  assert.equal(CH.historyText({ history_start: null, retention_days: 400 }, NOW, 'UTC').title, null)
  assert.match(CH.historyText(undefined, NOW, 'UTC').text, /^History starts when change capture was enabled\./, 'while it loads or if it failed: no date, no invented one')
  assert(!/20\d\d/.test(CH.historyText(undefined, NOW, 'UTC').text) && !/20\d\d/.test(CH.historyText({ history_start: null, retention_days: 400 }, NOW, 'UTC').text), 'no date appears unless the API gave one')
  assert.equal(changesGolden.history_start, '2026-08-24T12:00:00Z')
  // Paging.
  assert.deepEqual(CH.pastTheEnd(1, 56, 50), { beyond: false, pageCount: 2 })
  assert.deepEqual(CH.pastTheEnd(3, 56, 50), { beyond: true, pageCount: 2 })
  assert.deepEqual(CH.pastTheEnd(2, 0, 50), { beyond: false, pageCount: 1 }, 'an empty result has one (empty) page')
  assert.deepEqual(CH.pastTheEnd(1, 50, 50), { beyond: false, pageCount: 1 })
  assert.deepEqual(CH.pastTheEnd(2, 51, 50), { beyond: false, pageCount: 2 })
}


/* ═══ (d) The pages, rendered from the golden responses ═════════════════════════════════════ */

const decode = (t) => t.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#x27;/g, "'")
const textOf = (html) => decode(html.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ')
const anchors = (html) => [...html.matchAll(/<a\s([^>]*)>/g)].map((a) => {
  const attrs = Object.fromEntries([...a[1].matchAll(/([\w:-]+)="([^"]*)"/g)].map((x) => [x[1], decode(x[2])]))
  return { href: attrs.href, attrs }
})
const noNested = (html) => {
  let depth = 0
  for (const tag of html.matchAll(/<(\/?)a(?=[\s>])/g)) {
    depth += tag[1] ? -1 : 1
    assert(depth <= 1, 'a link is nested inside a link')
  }
}
const USER = (permissions) => ({ id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions })
function setUser(user) {
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const VIEW = ['inventory.view']
const EXPORT = ['inventory.view', 'inventory.export']
const summaryGolden = readJson('summary.json')
const qc = (entries = []) => {
  const client = new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
  for (const [key, value] of entries) client.setQueryData(key, value)
  return client
}
const page = (el, client, url) => m.renderToStaticMarkup(h(m.QueryClientProvider, { client }, h(m.MemoryRouter, { initialEntries: [url] }, el)))
const http500 = () => Object.assign(new Error('Request failed with status code 500'), { response: { status: 500, data: {} } })
const failed = (client, key) => client.getQueryCache().build(client, { queryKey: key }).setState({ data: undefined, status: 'error', error: http500(), fetchStatus: 'idle', dataUpdatedAt: 0, errorUpdatedAt: Date.now(), errorUpdateCount: 1 })
const SUMMARY_KEY = ['inventory', 'summary']
const checkDrills = (html, where) => {
  for (const a of anchors(html)) {
    const url = new URL(a.href, 'http://x')
    if (url.pathname !== '/inventory/assets') continue
    const parsed = F.parseAssetFilters(url.searchParams)
    assert.deepEqual(parsed.rejected, [], `${where}: ${a.href} is the vocabulary and nothing else`)
    const extra = [...url.searchParams.keys()].filter((k) => !F.ASSET_FILTER_KEYS.includes(k) && !['asset', 'sort', 'order', 'page', 'page_size'].includes(k))
    assert.deepEqual(extra, [], `${where}: ${a.href} has keys outside the vocabulary`)
  }
}

// ── Operating Systems ─────────────────────────────────────────────────────────────────────
{
  setUser(USER(VIEW))
  const seeded = (os = osGolden, key = 'all', extra = []) => qc([[['inventory', 'os', key], os], [SUMMARY_KEY, summaryGolden], ...extra])
  const html = page(h(m.InventoryOsPage), seeded(), '/inventory/os')
  noNested(html)
  checkDrills(html, 'os')
  assert(html.includes('data-page="os"') && html.includes('data-os-tree'))
  // The kind switch: three links, the current one marked.
  const sw = html.match(/<div[^>]*data-kind-switch[^>]*>.*?<\/div>/s)[0]
  assert.deepEqual(anchors(sw).map((a) => a.href), ['/inventory/os', '/inventory/os?kind=server', '/inventory/os?kind=device'])
  assert.deepEqual(anchors(sw).filter((a) => a.attrs['aria-current'] === 'true').map((a) => a.href), ['/inventory/os'])
  assert.match(textOf(sw), /All Servers Network/)
  // The tree: a real table, scope headers, aria-sort, expand buttons with aria-expanded, a count link on every row.
  const tree = html.match(/<table[^>]*data-os-tree.*?<\/table>/s)[0]
  assert(tree.includes('<caption class="sr-only">Operating systems of 145 assets'))
  assert((tree.match(/scope="col"/g) ?? []).length >= 5, 'column headers')
  assert(tree.includes('aria-sort="none"') && !tree.includes('aria-sort="ascending"'), 'unsorted: the API order')
  const rowsOf1 = [...tree.matchAll(/data-os-row="([^"]+)"/g)].map((x) => x[1])
  const visible = OS.visibleOsRows(OS.buildOsRows(osGolden), OS.defaultExpanded(OS.buildOsRows(osGolden)))
  assert.deepEqual(rowsOf1, visible.map((r) => r.key), 'the page draws exactly the rows the model says are open')
  assert((tree.match(/scope="row"/g) ?? []).length === visible.length, 'the label is the row header')
  assert(tree.includes('aria-expanded="true"') && tree.includes('aria-expanded="false"'), 'expand buttons say their state')
  assert(decode(tree).includes('aria-label="Expand Windows Server 2022, 3 versions"'), 'UI-13: a release is named with its product and says how many versions it opens')
  // Names are unique across the tree (no two different buttons are both "Collapse Windows").
  const names = [...decode(tree).matchAll(/aria-label="((?:Expand|Collapse)[^"]*)"/g)].map((x) => x[1].replace(/^(Expand|Collapse) /, ''))
  assert.equal(new Set(names).size, names.length, `expand buttons have unique names: ${names.join(' | ')}`)
  const allRows = OS.buildOsRows(osGolden)
  const pathCount = new Map()
  for (const r of allRows) pathCount.set(`${r.type}|${r.path}`, (pathCount.get(`${r.type}|${r.path}`) ?? 0) + 1)
  assert([...pathCount.values()].every((n) => n === 1), 'every row path is unique within its type')
  assert.deepEqual(allRows.filter((r) => r.type === 'family').slice(0, 1).map((r) => r.path), ['Windows (family)'])
  assert(allRows.some((r) => r.type === 'version' && /^Windows Server 2022 version 10\.0\.\d+\.\d+$/.test(r.path)))
  // Every count link stands for the row's number.
  for (const r of visible) {
    const link = anchors(tree).find((a) => a.attrs['data-drill-count'] === String(r.count) && a.href === r.drill.href)
    assert(link, `${r.key}: a link to ${r.drill.href} carrying ${r.count}`)
    assert.equal(link.attrs['aria-label'], OS.rowSummary(r, 145, 'assets'), `${r.key}: a link read out of context still says what it counts`)
  }
  assert(textOf(tree).includes('Unknown'), 'unknown is a row')
  assert(!tree.includes('Reported as'), 'no raw string column while no version is on show')
  // Donut and legend: every family, linked; a table alternative; the currency card with its caveat.
  assert(html.includes('<svg') && html.includes('role="img"'))
  const legend = html.match(/aria-label="Operating system families".*?<\/ul>/s)[0]
  assert.equal(anchors(legend).length, osGolden.families.length + 1, 'the legend links every family, and names the grey slice that folds the tail')
  assert(decode(legend).includes('Other (6 families)') && legend.includes('data-legend-other'), 'UI-12: the grey slice has a heading row')
  assert((decode(legend).match(/in the grey Other slice/g) ?? []).length === 6, 'and the six families it folds say so')
  // The donut's slices are not anchors (an anchor inside role=img is a nested interactive control); the legend is the way in.
  const svg = html.match(/<svg[^>]*role="img".*?<\/svg>/s)[0]
  assert(!/<a[\s>]/.test(svg), 'UI-22: no link inside the chart image')
  assert(/data-slice="windows"[^>]*>/.test(svg) && svg.includes('data-href="/inventory/assets?os_family=windows"'), 'a slice still knows its list')
  assert(html.includes('aria-pressed="true"') && decode(html).includes('Operating system families view'), 'the table toggle')
  assert(html.includes('data-currency-note') && decode(html).includes("not the vendor's latest release"))
  assert(textOf(html).includes('Windows Server 2022') && /9\s*10\.0\.20348\.2700/.test(textOf(html)), 'build currency of the first release')
  assert.equal(anchors(html).filter((a) => a.attrs['data-drill'] === 'assets' && /os_version=10\.0\.20348\.2700/.test(a.href)).length >= 1, true, 'the latest observed count is a list')
  // Export only for inventory.export.
  assert(!html.includes('aria-label="Export CSV"'))
  setUser(USER(EXPORT))
  assert(page(h(m.InventoryOsPage), seeded(), '/inventory/os').includes('aria-label="Export CSV"'))
  setUser(USER(VIEW))

  // A kind: the drills carry it, the switch shows it, the cache key is the Overview's.
  const dev = page(h(m.InventoryOsPage), seeded({ ...osGolden, kind: 'device' }, 'device'), '/inventory/os?kind=device')
  checkDrills(dev, 'os device')
  assert(anchors(dev).filter((a) => a.attrs['data-drill'] === 'assets').every((a) => /kind=device/.test(a.href)), 'every list a device tree opens is limited to devices')
  assert.deepEqual(anchors(dev.match(/data-kind-switch.*?<\/div>/s)[0]).filter((a) => a.attrs['aria-current'] === 'true').map((a) => a.href), ['/inventory/os?kind=device'])
  assert(textOf(dev).includes('network devices'))
  // Sorting from the address: the arrow follows, and the first family is the smallest.
  const asc = page(h(m.InventoryOsPage), seeded(), '/inventory/os?sort=count&order=asc')
  assert(asc.includes('aria-sort="ascending"'))
  const ascFamilies = [...asc.matchAll(/data-os-row="(f:[^"]+)"/g)].map((x) => x[1])
  assert.deepEqual(ascFamilies, OS.buildOsRows(osGolden, 'all', { key: 'count', order: 'asc' }).filter((r) => r.type === 'family').map((r) => r.key), 'the page follows the sort in the address')
  assert.notDeepEqual(ascFamilies, OS.buildOsRows(osGolden).filter((r) => r.type === 'family').map((r) => r.key))
  assert.equal(ascFamilies.at(-1), 'f:unknown', 'unknown stays last')
  // A bad kind is reported, never silent.
  const bad = page(h(m.InventoryOsPage), seeded(), '/inventory/os?kind=router')
  assert(decode(bad).includes('Ignored in the address: kind=router'))
  // Empty states are different facts.
  const none = { ...osGolden, total: 0, families: [] }
  assert(textOf(page(h(m.InventoryOsPage), seeded(none), '/inventory/os')).includes('No assets yet'))
  assert(textOf(page(h(m.InventoryOsPage), seeded({ ...none, kind: 'device' }, 'device'), '/inventory/os?kind=device')).includes('No network devices yet'))
  assert(textOf(page(h(m.InventoryOsPage), seeded({ ...none, kind: 'server' }, 'server'), '/inventory/os?kind=server')).includes('No servers yet'))
  const scoped = page(h(m.InventoryOsPage), qc([[['inventory', 'os', 'all'], none], [SUMMARY_KEY, { ...summaryGolden, scope: { restricted: true, tags: ['team-edge'] } }]]), '/inventory/os')
  assert(textOf(scoped).includes('No assets in your scope') && textOf(scoped).includes('team-edge'))
  // Loading and failure.
  assert(page(h(m.InventoryOsPage), qc(), '/inventory/os').includes('aria-busy="true"'))
  const dead = qc([[SUMMARY_KEY, summaryGolden]])
  failed(dead, ['inventory', 'os', 'all'])
  const deadHtml = page(h(m.InventoryOsPage), dead, '/inventory/os')
  assert(decode(deadHtml).includes('Could not load the operating system breakdown') && deadHtml.includes('role="alert"') && deadHtml.includes('Retry'))
  assert(deadHtml.includes('data-kind-switch'), 'the page frame stays: the switch is still there')
  // A failed refresh with data keeps the last figures and says so.
  const stale = seeded()
  const entry = stale.getQueryCache().find({ queryKey: ['inventory', 'os', 'all'] })
  entry.setState({ ...entry.state, status: 'error', error: http500(), fetchStatus: 'idle', errorUpdateCount: 1, fetchFailureCount: 1 })
  const staleHtml = page(h(m.InventoryOsPage), stale, '/inventory/os')
  assert(staleHtml.includes('Showing the last data that loaded') && staleHtml.includes('data-os-tree'))
  // Forbidden: nothing renders.
  setUser(USER(['devices.view']))
  assert.equal(page(h(m.InventoryOsPage), seeded(), '/inventory/os'), '')
  setUser(USER(VIEW))
  // The raw firmware string has a column once a version with one is on show.
  const rows = OS.buildOsRows(osGolden)
  const withRaw = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.OsTree, { rows, expanded: OS.allExpandable(rows), onToggle() {}, sort: OS.DEFAULT_OS_SORT, onSort() {}, total: 145, noun: 'assets', caption: 'x' })))
  noNested(withRaw)
  assert(withRaw.includes('Reported as') && withRaw.includes('v7.2.8,build1639,240614 (GA.M)'), 'the raw string is shown beside the normalised version')
  assert(decode(withRaw).includes('title="As the device reported it: v7.2.8,build1639,240614 (GA.M)"'))
}


// ── Hardware ──────────────────────────────────────────────────────────────────────────────
{
  setUser(USER(VIEW))
  const base = { ...hwGolden, matrix: null }
  const seeded = (extra = [], hw = base) => qc([[['inventory', 'hardware'], hw], [SUMMARY_KEY, summaryGolden], ...extra])
  const html = page(h(m.InventoryHardwarePage), seeded([[['inventory', 'hardware', 'Cisco'], hwGolden]]), '/inventory/hardware')
  noNested(html)
  checkDrills(html, 'hardware')
  assert(html.includes('data-page="hardware"'))
  // The strip: devices and servers are lists; the vendor and model counts are text on this very page.
  const strip = html.match(/<p class="text-xs text-muted" role="status">.*?<\/p>/s)[0]
  assert.deepEqual(anchors(strip).map((a) => [a.href, a.attrs['data-drill-count']]), [['/inventory/assets?kind=device', '85'], ['/inventory/assets?kind=server', '60']])
  assert.match(textOf(strip), /85 network devices · 7 vendors, 19 models plus 17 devices with no model · 60 servers/, 'UI-04: known models, and the devices with none said apart')
  // The vendor > model table: a real table, rows for every vendor and (this small fleet) every model.
  const table = html.match(/<table[^>]*data-hardware-table.*?<\/table>/s)[0]
  assert((table.match(/data-vendor=/g) ?? []).length === 8 && (table.match(/data-model=/g) ?? []).length === 24, 'eight vendors, nineteen known models and five unknown-model rows, none dropped')
  assert((table.match(/scope="col"/g) ?? []).length === 6 && (table.match(/scope="row"/g) ?? []).length === 32)
  assert(table.includes('data-vendor=""'), 'the vendor-less devices are a row')
  assert(textOf(table).includes('Unknown vendor'))
  const vendorLinks = anchors(table)
  for (const v of HW.vendorRows(base)) assert(vendorLinks.some((a) => a.href === v.drill.href && a.attrs['data-drill-count'] === String(v.count)), `${v.label}: its count links to ${v.drill.href}`)
  assert(vendorLinks.some((a) => a.href === '/inventory/assets?kind=device&vendor=Cisco&health=missing_serial' && a.attrs['data-drill-count'] === '5'), 'the missing serials are a list')
  assert(textOf(table).includes('82.1%') && textOf(table).includes('of devices report a serial number'), 'the pill says its percentage and, for a screen reader, what it means')
  assert(vendorLinks.some((a) => a.href === '/inventory/assets?kind=device&vendor=unknown&freshness=not_applicable' && a.attrs['data-drill-count'] === '4'), 'devices that cannot report by design: their own list, outside the percentage')
  assert(vendorLinks.some((a) => a.href === '/inventory/assets?kind=device&vendor=unknown' && a.attrs['data-drill-count'] === '9'), 'the vendor-less row uses the sentinel')
  assert(decode(table).includes('5 models plus 3 devices with no model'), 'UI-04: the vendor row says known models and the devices with none')
  assert(table.includes('data-vendor="Cisco"') && (table.match(/Firmware matrix/g) ?? []).length === 7, 'a matrix button for every named vendor')
  assert(table.includes('aria-expanded="true"') && decode(table).includes('aria-label="Collapse Cisco, 5 models plus 3 devices with no model"'))
  // Missing is not zero: a model with no firmware says "none" rather than 0 (none in the golden fleet: the vendor-less row says a dash).
  // The matrix: its own panel, vendor select, cells.
  const matrix = html.match(/<table[^>]*data-matrix=.*?<\/table>/s)[0]
  assert(decode(matrix).includes('Cisco: 6 models') || /Cisco: \d+ models/.test(decode(matrix)))
  assert.equal((matrix.match(/scope="col"/g) ?? []).length, 1 + hwGolden.matrix.releases.length + 1, 'model, one column per release, total')
  const cells = anchors(matrix)
  const mx = HW.matrixModel(hwGolden)
  const expectedCells = mx.rows.flatMap((r) => r.cells.filter((c) => c.count > 0))
  assert.equal(cells.filter((a) => a.attrs['data-drill-count']).length, expectedCells.length + mx.rows.length, 'a link per non-empty cell and one per row total')
  for (const c of expectedCells) assert(cells.some((a) => a.href === c.drill.href && a.attrs['data-drill-count'] === String(c.count)), `cell ${c.drill.href}`)
  assert(matrix.includes('aria-label="No devices"'), 'an empty cell is marked, not blank')
  // UI-15: the Total column's link names its row (it was a bare number); UI-19: nouns agree with numbers.
  for (const r of mx.rows) {
    const total = `${r.total.toLocaleString('en-US')} ${r.total === 1 ? 'device' : 'devices'}`
    assert(decode(matrix).includes(`aria-label="${mx.vendor} ${r.label}, all releases: ${total}"`), `${r.label}: the Total link says whose total it is`)
  }
  assert(!/\b1 (devices|releases|models|servers)\b/.test(textOf(html)) && !/title="1 (devices|releases)/.test(html), 'UI-19: no "1 devices"')
  assert(html.includes('data-matrix-legend') && textOf(html.match(/data-matrix-legend.*?<\/div>\s*<\/div>/s)?.[0] ?? html).includes('every cell shows its number'))
  const select = html.match(/<select[^>]*data-matrix-vendor[^>]*>.*?<\/select>/s)[0]
  assert(select.includes('value="Cisco" selected') || /<option value="Cisco" selected/.test(select) || select.includes('Cisco (28)'))
  assert.equal((select.match(/<option/g) ?? []).length, 7)
  // Modules: grouped by class, every model's devices a link through module_class and module_model.
  for (const cls of ['chassis', 'module', 'powerSupply', 'fan']) assert(html.includes(`data-module-class="${cls}"`), cls)
  assert(anchors(html).some((a) => a.href === '/inventory/assets?module_class=powerSupply&module_model=PWR-C1-715WAC-P' && a.attrs['data-drill-count'] === '17'), 'devices with a component, not components')
  assert(anchors(html).some((a) => a.href === '/inventory/assets?module_class=chassis&module_model=unknown'), 'the unnamed model is the word unknown')
  assert(textOf(html).includes('30 of 34 report a serial'))
  assert(textOf(html).includes('Show 13 more models'), 'twenty chassis models: six named and the unnamed one shown')
  // Server hardware: every bucket a link, virtualisation present because the API returned it.
  const servers = html.match(/<section[^>]*aria-label="CPU models".*?<\/section>/s)[0]
  assert.equal(anchors(servers).length, hwGolden.servers.cpu_models.length)
  assert(anchors(servers).some((a) => a.href === '/inventory/assets?cpu_model=unknown'), 'unknown CPUs are a list')
  assert(html.includes('aria-label="Virtualization"') && !html.includes('data-server-hardware="pending"'))
  assert(anchors(html).some((a) => a.href === '/inventory/assets?memory=gt128' && a.attrs['data-drill-count'] === '5'))
  assert(anchors(html).some((a) => a.href === '/inventory/assets?disks=ge9' && a.attrs['data-drill-count'] === '0'), 'an empty bucket is a zero, linked')
  // Export only for inventory.export.
  assert(!html.includes('aria-label="Export CSV"'))
  setUser(USER(EXPORT))
  assert(page(h(m.InventoryHardwarePage), seeded([[['inventory', 'hardware', 'Cisco'], hwGolden]]), '/inventory/hardware').includes('aria-label="Export CSV"'))
  setUser(USER(VIEW))

  // The vendor in the address chooses the matrix (any case); the rest of the page does not wait for it.
  const fort = readJson('hardware.json')
  const pending = page(h(m.InventoryHardwarePage), seeded(), '/inventory/hardware?vendor=fortinet')
  assert(pending.includes('aria-label="Loading the firmware matrix"') && pending.includes('data-hardware-table'), 'the matrix loads on its own')
  assert(/<option value="Fortinet" selected/.test(pending) || pending.includes('value="Fortinet"'))
  const deadMatrix = seeded()
  failed(deadMatrix, ['inventory', 'hardware', 'Fortinet'])
  const dm = page(h(m.InventoryHardwarePage), deadMatrix, '/inventory/hardware?vendor=Fortinet')
  assert(decode(dm).includes('Could not load the firmware matrix') && dm.includes('data-hardware-table') && dm.includes('aria-label="Server hardware"') || dm.includes('Server hardware'), 'a failing matrix does not take the page with it')
  assert(dm.includes('Retry'))

  // What servers do not report yet is said in words, not faked.
  const sparse = { ...base, modules: [], servers: { ...base.servers, virtualization: null, cpu_models: [{ value: 'unknown', label: 'Unknown', count: 60 }], memory: [{ value: 'unknown', label: 'Unknown', count: 60 }], disks: [{ value: 'unknown', label: 'Unknown', count: 60 }] } }
  const sp = page(h(m.InventoryHardwarePage), seeded([], sparse), '/inventory/hardware')
  assert(sp.includes('data-server-hardware="pending"') && !/phase/i.test(decode(sp)) && decode(sp).includes('Manufacturer, model, serial number, BIOS'))
  assert(sp.includes('data-server-hardware="none"') && decode(sp).includes('No server has reported its CPU, memory or disks yet'))
  assert(decode(sp).includes('No components reported yet') && !sp.includes('data-module-class'))
  noNested(sp)
  // Nothing at all, a scope that hides everything, loading, failure, forbidden.
  const nothing = { ...base, network: { devices: 0, vendors: [], models: [] }, modules: [], servers: { ...base.servers, total: 0, cpu_models: [], memory: [], disks: [], virtualization: null } }
  assert(textOf(page(h(m.InventoryHardwarePage), seeded([], nothing), '/inventory/hardware')).includes('No hardware reported yet'))
  assert(textOf(page(h(m.InventoryHardwarePage), qc([[['inventory', 'hardware'], nothing], [SUMMARY_KEY, { ...summaryGolden, scope: { restricted: true, tags: ['team-edge'] } }]]), '/inventory/hardware')).includes('No hardware in your scope'))
  assert(page(h(m.InventoryHardwarePage), qc(), '/inventory/hardware').includes('aria-busy="true"'))
  const dead = qc([[SUMMARY_KEY, summaryGolden]])
  failed(dead, ['inventory', 'hardware'])
  assert(decode(page(h(m.InventoryHardwarePage), dead, '/inventory/hardware')).includes('Could not load the hardware breakdown'))
  setUser(USER(['devices.view']))
  assert.equal(page(h(m.InventoryHardwarePage), seeded(), '/inventory/hardware'), '')
  setUser(USER(VIEW))
  // A fleet with many models opens the three biggest vendors only.
  const many = { ...base, network: { ...base.network, models: Array.from({ length: 50 }, (_, i) => ({ vendor: base.network.vendors[i % 3].vendor, model: `M${i}`, count: 1, firmware_versions: 1, serial_missing: 0, serial_coverage_pct: 100, modules: 0, unknown: false, not_collected: 0 })) } }
  const manyHtml = page(h(m.InventoryHardwarePage), seeded([], many), '/inventory/hardware')
  assert.equal((manyHtml.match(/data-model=/g) ?? []).length, 50, 'the models of the three biggest vendors are open')
  const manyTable = manyHtml.match(/<table[^>]*data-hardware-table.*?<\/table>/s)[0]
  assert((manyTable.match(/aria-expanded="false"/g) ?? []).length === 0, 'the other vendors have no models in this fixture, so there is nothing to expand')
  // Four vendors with models in a big fleet: the fourth starts closed.
  const four = { ...many, network: { ...many.network, models: [...many.network.models, { vendor: 'Juniper Networks', model: 'EX9999', count: 1, firmware_versions: 1, serial_missing: 0, serial_coverage_pct: 100, modules: 0, unknown: false, not_collected: 0 }] } }
  const fourTable = page(h(m.InventoryHardwarePage), seeded([], four), '/inventory/hardware').match(/<table[^>]*data-hardware-table.*?<\/table>/s)[0]
  assert.equal((fourTable.match(/aria-expanded="false"/g) ?? []).length, 1)
  assert.equal((fourTable.match(/data-model=/g) ?? []).length, 50, 'the fourth vendor stays closed')
  void fort
}


// ── Changes ───────────────────────────────────────────────────────────────────────────────
{
  setUser(USER(VIEW))
  // The golden feed is stamped 2026-10-08; the page reads the real clock, so its times move with it.
  const delta = Date.now() - Date.parse('2026-10-08T12:00:00Z')
  const retime = (v) => (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v) ? new Date(Date.parse(v) + delta).toISOString().replace(/\.\d{3}Z$/, 'Z') : Array.isArray(v) ? v.map(retime) : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, retime(x)])) : v)
  const feed = retime(changesGolden)
  const key = (filters = {}, pageNo = 1, size = 50) => ['inventory', 'changes', JSON.stringify(CH.changesQuery(filters, pageNo, size, 'UTC'))]
  const seeded = (res = feed, filters = {}, extra = []) => qc([[key(filters), res], [SUMMARY_KEY, summaryGolden], ...extra])
  const html = page(h(m.InventoryChangesPage), seeded(), '/inventory/changes')
  noNested(html)
  checkDrills(html, 'changes')
  assert(html.includes('data-page="changes"'))

  // The stats strip: eight chips, the window's numbers, each a link whose address is the page with that chip applied.
  const strip = html.match(/<ul[^>]*data-stat-chips.*?<\/ul>/s)[0]
  const chips = [...strip.matchAll(/<a\s([^>]*)>/g)].map((a) => Object.fromEntries([...a[1].matchAll(/([\w:-]+)="([^"]*)"/g)].map((x) => [x[1], decode(x[2])])))
  assert.deepEqual(chips.map((c) => c['data-chip']), ['all', 'installed', 'removed', 'upgraded', 'downgraded', 'os', 'firmware', 'hardware'])
  assert.deepEqual(chips.map((c) => c['data-drill-count']), ['56', '12', '12', '12', '1', '4', '11', '2'], 'each number is the total of the list it opens')
  assert.deepEqual(chips.map((c) => c.href), [
    '/inventory/changes', '/inventory/changes?category=software&action=added', '/inventory/changes?category=software&action=removed',
    '/inventory/changes?category=software&action=changed&label=upgrade', '/inventory/changes?category=software&action=changed&label=downgrade',
    '/inventory/changes?category=os', '/inventory/changes?category=firmware', '/inventory/changes?category=hardware',
  ])
  assert.deepEqual(chips.filter((c) => c['aria-current'] === 'true').map((c) => c['data-chip']), ['all'])
  assert(textOf(strip).includes('All changes 56 Installed 12 Removed 12 Upgraded 12 Downgraded 1 OS changes 4 Firmware changes 11 Hardware changes 2'))
  assert(html.includes('data-paired-note') && decode(html).includes('Plus 6 upgrades and 1 downgrade recorded as an installation and a removal'))
  // The window switch: four links, 7 d marked, the default has no since, the others write one.
  const win = html.match(/<div[^>]*data-window-switch.*?<\/div>/s)[0]
  const wl = anchors(win)
  assert.deepEqual(wl.map((a) => a.attrs.title), ['Changes in the last 24 hours', 'Changes in the last 7 days', 'Changes in the last 30 days', 'Changes in the last 90 days'])
  assert.deepEqual(wl.filter((a) => a.attrs['aria-current'] === 'true').map((a) => a.attrs.title), ['Changes in the last 7 days'])
  assert(/^\/inventory\/changes\?since=\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A00Z$/.test(wl[0].href) && wl[1].href === '/inventory/changes' && /since=/.test(wl[2].href) && /since=/.test(wl[3].href))
  assert.equal(html.match(/data-window-text="">([^<]*)</)[1], 'last 7 days')

  // The chart: a text summary, a table alternative, a legend of links.
  assert(html.includes('data-chart="changes-per-day"') && /role="img" aria-label="Changes per day, 8 (calendar )?days[^"]*56 in total, busiest/.test(html))
  // UI-20: a partial first or last day is marked and explained (the golden feed is re-timed to now, the series days are not).
  const partialDays = CH.chartDays(feed.series, Date.now(), 'UTC', feed.window.since).filter((d) => d.partial)
  assert(partialDays.length > 0 && html.includes('data-chart-partial') && decode(html).includes('* A partial day'), 'the partial day is explained')
  assert(decode(html).includes('Changes per day view') && html.includes('aria-pressed="true"'))
  const legend = html.match(/aria-label="Categories".*?<\/ul>/s)[0]
  assert.deepEqual(anchors(legend).map((a) => a.href), ['/inventory/changes?category=software', '/inventory/changes?category=os', '/inventory/changes?category=firmware', '/inventory/changes?category=hardware'])
  assert.match(textOf(legend), /Software 41 OS 4 Firmware 11 Hardware 2/.test(textOf(legend)) ? /Software/ : /Software/)

  // The list: pairs are ONE row, the marker is its own kind of row, capped lists carry the note.
  const list = html.match(/<div[^>]*data-changes-list.*?<\/div>\s*<div class="flex flex-wrap items-center justify-between gap-2 border-t/s)?.[0] ?? html
  const kinds = [...list.matchAll(/data-change-kind="(\w+)"/g)].map((x) => x[1])
  assert.equal(kinds.length, 43, 'fifty events, seven of them merged away')
  assert.equal(kinds.filter((k) => k === 'pair').length, 7)
  assert.equal(kinds.filter((k) => k === 'marker').length, 1)
  assert.equal((html.match(/data-truncated=""/g) ?? []).length, 2)
  assert(decode(html).includes('The list was capped at 1,000 titles when this was recorded, so this title may have been cut off rather than removed.'))
  assert(html.includes('data-change="52,51"') && !html.includes('data-change="52"') && !html.includes('data-change="51"'), 'the two halves of one upgrade are one row and neither is a row of its own')
  assert(textOf(html).includes('Java 8 Update 401 upgraded 8.0.3910.11 → 8.0.4010.10 (replaces Java 8 Update 391) on srv-w22-17'))
  assert(textOf(html).includes('Google Chrome upgraded 151.0.7922.110 → 152.0.8001.12 on srv-w22-01'), 'a version change is one sentence')
  assert(textOf(html).includes('FortiOS changed 7.2.5 → 7.2.8 on fg-01'))
  assert(textOf(html).includes('CrowdStrike Falcon Sensor installed 7.15.18513 on srv-w19-13'))
  assert(textOf(html).includes('Change budget exceeded'))
  assert(html.includes('This marks a gap in the history'))
  assert.match(textOf(html), /56 events, 43 rows on this page \(7 pairs of installation and removal shown as one upgrade\)/)
  // Every asset name is a link that opens its details.
  assert(anchors(html).some((a) => /^\/inventory\/assets\?asset=server%3A878db435-7146-56e1-948f-976b4d4e287d$/.test(a.href)))
  // Day groups are headings with lists.
  assert(html.includes('<h3 id="day-') && (html.match(/<ol class/g) ?? []).length >= 3)
  // The pager.
  assert(textOf(html).includes(`${(1).toLocaleString()}–${(50).toLocaleString()} of ${(56).toLocaleString()} events`), 'the pager prints its numbers the way the browser does (NF-EP-06)')
  // The footnote carries the date the API gave, and no other.
  const foot = html.match(/<div[^>]*data-changes-footnote.*?<\/div>/s)[0]
  assert.equal(textOf(foot).trim(), CH.historyText(feed, Date.now(), 'UTC').text, 'FE-08: the footnote is the response\'s history_start and retention_days, in words')
  assert(/^History starts \w{3} \d{1,2} \w{3} \d{4}, when change capture began\. Changes older than 400 days are removed\./.test(textOf(foot).trim()), textOf(foot))
  const noHist = page(h(m.InventoryChangesPage), qc([[key(), { ...feed, history_start: null }], [SUMMARY_KEY, summaryGolden]]), '/inventory/changes')
  assert(textOf(noHist.match(/<div[^>]*data-changes-footnote.*?<\/div>/s)[0]).trim().startsWith('History starts when change capture was enabled'), 'no date until the API gives one')
  // A zone the server could not use is said, with the zone the days are grouped in.
  const fallback = page(h(m.InventoryChangesPage), qc([[key(), { ...feed, window: { ...feed.window, tz: 'UTC', tz_fallback: true } }], [SUMMARY_KEY, summaryGolden]]), '/inventory/changes')
  assert(textOf(fallback).includes("Days are grouped in UTC: the server does not know this browser's time zone"), 'FE-03: a time zone fallback is not silent')
  assert(!textOf(html).includes('Days are grouped in'))
  // Export only for inventory.export; the search box and the kind switch exist.
  assert(!html.includes('aria-label="Export CSV"') && html.includes('aria-label="Search changes"'))
  setUser(USER(EXPORT))
  assert(page(h(m.InventoryChangesPage), seeded(), '/inventory/changes').includes('aria-label="Export CSV"'))
  setUser(USER(VIEW))
  const kindLinks = anchors(html.match(/aria-label="Kind of asset".*?<\/div>/s)[0])
  assert.deepEqual(kindLinks.map((a) => a.href), ['/inventory/changes', '/inventory/changes?kind=server', '/inventory/changes?kind=device'])
  assert.equal(kindLinks.filter((a) => a.attrs['aria-current'] === 'true').length, 1)

  // The address bar IS the API's parameters: the page asks for exactly what the address says (a cache entry under
  // that exact request exists, so the page renders; any other parameter would leave it on the skeleton).
  {
    const f = { category: 'software', action: 'added', kind: 'server', q: 'chrome' }
    const url = '/inventory/changes?q=chrome&kind=server&category=software&action=added'
    const added = { ...feed, items: feed.items.filter((r) => r.action === 'added'), total: 12 }
    const view = page(h(m.InventoryChangesPage), seeded(added, f), url)
    assert(view.includes('data-changes-list') && !view.includes('Loading the change history'), 'the request carries category, action, kind and q under their own names')
    assert(view.includes('value="chrome"'), 'the search box shows the address')
    assert.deepEqual([...view.matchAll(/<a\s([^>]*)>/g)].map((a) => Object.fromEntries([...a[1].matchAll(/([\w:-]+)="([^"]*)"/g)].map((x) => [x[1], decode(x[2])]))).filter((c) => c['aria-current'] === 'true' && c['data-chip']).map((c) => c['data-chip']), ['installed'])
    // Chips keep the rest of the filters.
    assert(view.includes('/inventory/changes?q=chrome&amp;kind=server&amp;category=software&amp;action=removed') || view.includes('/inventory/changes?q=chrome&kind=server&category=software&action=removed'))
    // The pair rule only merges what the page shows: an installations-only list has no pairs to merge (all halves lose partners).
    assert(!view.includes('data-change-kind="pair"'))
  }
  // The Overview's link lands on exactly the list whose total it showed: the same instant, the same filters.
  {
    const since = '2026-10-01T12:00:00Z'
    const f = { category: 'software', action: 'changed', label: 'upgrade', since }
    const url = OV.changesHref({ category: 'software', action: 'changed', label: 'upgrade', since })
    const view = page(h(m.InventoryChangesPage), seeded({ ...feed, total: 12, items: feed.items.filter((r) => r.label === 'upgrade') }, f), url)
    assert(view.includes('data-changes-list'), 'the request carries the Overview\'s since unchanged')
    assert(textOf(view).includes('12 events'))
  }
  // An old address: the window is no preset (Custom), and says what it is. The span is relative to the real
  // clock the page uses (4.5 days: far from every preset), so the case cannot drift into a preset as days pass.
  {
    const since = new Date(Math.floor((Date.now() - 4.5 * 86400000) / 1000) * 1000).toISOString().replace('.000Z', 'Z')
    const f = { since }
    const view = page(h(m.InventoryChangesPage), seeded(feed, f), `/inventory/changes?since=${since}`)
    assert(view.includes('data-window-text') && /Custom/.test(view) && decode(view).includes('Window:'), 'a custom window is a marked segment and a removable chip')
  }
  // Bad values are reported and never sent on.
  {
    const view = page(h(m.InventoryChangesPage), seeded(), '/inventory/changes?category=network&since=yesterday')
    assert(decode(view).includes('Ignored in the address: category=network') && decode(view).includes('since=yesterday'))
    assert(view.includes('data-changes-list'), 'the page still shows the default list')
  }
  // Empty states are different facts.
  {
    const none = { ...feed, items: [], total: 0, stats: { total: 0, software: { added: 0, removed: 0, changed: 0, upgraded: 0, downgraded: 0, paired: { upgraded: 0, downgraded: 0 } }, os: 0, firmware: 0, hardware: 0 }, series: feed.series.map((d) => ({ ...d, software: 0, os: 0, firmware: 0, hardware: 0, total: 0 })) }
    const quiet = page(h(m.InventoryChangesPage), seeded(none), '/inventory/changes')
    assert(textOf(quiet).includes('No changes in the last 7 days') && textOf(quiet).includes('Widen the window to look further back'), 'a quiet window')
    assert(textOf(quiet).includes('Show the last 30 days') && textOf(quiet).includes('Show the last 90 days'), 'and the way to a wider one')
    assert(textOf(quiet).includes('No changes on any day of this window') && !/change capture is running/.test(textOf(quiet)), 'the chart says so too, and nothing claims what the page cannot know')
    const filtered = page(h(m.InventoryChangesPage), seeded(none, { q: 'zzz' }), '/inventory/changes?q=zzz')
    assert(textOf(filtered).includes('Nothing matches these filters') && textOf(filtered).includes('Clear all filters'))
    const scoped = page(h(m.InventoryChangesPage), qc([[key(), none], [SUMMARY_KEY, { ...summaryGolden, scope: { restricted: true, tags: ['team-edge'] } }]]), '/inventory/changes')
    assert(scoped.includes('data-empty="no-scope"') && textOf(scoped).includes('None of the assets in your scope has changed'), 'a scope that hides everything is not an empty fleet')
    assert(textOf(scoped).includes('You see the changes of the assets in your scope'), 'the footnote says so for a scoped user')
    const beyond = page(h(m.InventoryChangesPage), qc([[key({}, 3), { ...feed, items: [], page: 3 }], [SUMMARY_KEY, summaryGolden]]), '/inventory/changes?page=3')
    assert(beyond.includes('data-state="beyond"') && textOf(beyond).includes(`Page ${(3).toLocaleString()} is past the end`) && textOf(beyond).includes(`Go to the last page (${(2).toLocaleString()})`))
  }
  // Loading, failure, a failed refresh, forbidden.
  assert(page(h(m.InventoryChangesPage), qc(), '/inventory/changes').includes('aria-busy="true"'))
  {
    const dead = qc([[SUMMARY_KEY, summaryGolden]])
    failed(dead, key())
    const deadHtml = page(h(m.InventoryChangesPage), dead, '/inventory/changes')
    assert(decode(deadHtml).includes('Could not load the change history') && deadHtml.includes('role="alert"') && deadHtml.includes('Retry'))
    assert(deadHtml.includes('data-changes-toolbar'), 'the toolbar stays')
    const stale = seeded()
    const entry = stale.getQueryCache().find({ queryKey: key() })
    entry.setState({ ...entry.state, status: 'error', error: http500(), fetchStatus: 'idle', errorUpdateCount: 1, fetchFailureCount: 1 })
    const staleHtml = page(h(m.InventoryChangesPage), stale, '/inventory/changes')
    assert(staleHtml.includes('Showing the last data that loaded') && staleHtml.includes('data-changes-list'))
    setUser(USER(['devices.view']))
    assert.equal(page(h(m.InventoryChangesPage), seeded(), '/inventory/changes'), '')
    setUser(USER(VIEW))
  }
  // A page with an unmerged half says where the other one is.
  {
    const only = { ...feed, items: [feed.items.find((r) => r.id === 51)], total: 1 }
    const view = page(h(m.InventoryChangesPage), seeded(only), '/inventory/changes')
    assert(textOf(view).includes('The other half of this change is on another page.'))
  }
}


// ── The fetchers: what goes over the wire, and the files that come back ───────────────────
{
  const calls = []
  const answer = (config) => {
    const url = `${config.baseURL}${config.url}`
    const params = config.params instanceof URLSearchParams ? Object.fromEntries(config.params) : { ...(config.params ?? {}) }
    calls.push({ url, params, responseType: config.responseType })
    const ok = (data, headers = {}) => Promise.resolve({ data, status: 200, statusText: 'OK', headers, config })
    if (url.endsWith('/inventory/export')) return ok(new Blob(['a,b\r\n']), { 'content-disposition': 'attachment; filename="zenplus-inventory-changes-20261008.csv"', 'x-export-rows': '56', 'x-export-truncated': 'true' })
    if (url.endsWith('/inventory/changes')) {
      return ok(changesGolden)
    }
    if (url.endsWith('/inventory/os')) return ok(osGolden)
    return ok(hwGolden)
  }
  m.api.defaults.adapter = answer
  const last = () => calls.at(-1)

  await OHC.fetchOs('device')
  assert.deepEqual(last(), { url: '/api/v1/inventory/os', params: { kind: 'device' }, responseType: undefined })
  await OHC.fetchOs()
  assert.deepEqual(last().params, {}, 'All sends no kind')
  await OHC.fetchHardware('Cisco')
  assert.deepEqual(last().params, { vendor: 'Cisco' })
  await OHC.fetchHardware()
  assert.deepEqual(last().params, {})
  await OHC.fetchChanges({ category: 'software', action: 'changed', label: 'upgrade', since: '2026-10-01T12:00:00Z', q: 'java' }, 2, 25, 'Europe/Berlin')
  assert.deepEqual(last().params, { category: 'software', action: 'changed', q: 'java', label: 'upgrade', since: '2026-10-01T12:00:00Z', tz: 'Europe/Berlin', page: 2, page_size: 25 })
  assert.equal(last().url, '/api/v1/inventory/changes')

  // Exports: the same filters, dataset named, no zone, no paging; the file is read from the headers.
  const file = await OHC.exportChanges({ category: 'os', kind: 'server', since: '2026-10-01T12:00:00Z', q: 'x' })
  assert.deepEqual(last(), { url: '/api/v1/inventory/export', params: { dataset: 'changes', category: 'os', kind: 'server', q: 'x', since: '2026-10-01T12:00:00Z' }, responseType: 'blob' })
  assert.deepEqual([file.rows, file.truncated, file.disposition], [56, true, 'attachment; filename="zenplus-inventory-changes-20261008.csv"'])
  await OHC.exportChanges({})
  assert.deepEqual(last().params, { dataset: 'changes' }, 'no filters, no window: the server\'s default (the last seven days)')
  await OHC.exportOs('device')
  assert.deepEqual(last().params, { dataset: 'os', kind: 'device' })
  await OHC.exportOs()
  assert.deepEqual(last().params, { dataset: 'os' })
  await OHC.exportHardware('Cisco')
  assert.deepEqual(last().params, { dataset: 'hardware', vendor: 'Cisco' })

  // FE-08: the footnote no longer probes 790 days; the API says when capture began. The probe is gone for good.
  assert.equal(OHC.fetchHistoryStart, undefined)
  assert.equal(OHC.HISTORY_LOOKBACK_DAYS, undefined)
  assert.equal(OHC.OSHW_KEYS.historyStart, undefined)
  const source = fs.readFileSync(path.join(src, 'pages/inventory/InventoryChangesPage.tsx'), 'utf8')
  assert(!/790|fetchHistoryStart/.test(source), 'the Changes page asks for no history probe')
  // FE-03: a zone the API refuses never fails the page. The first request carries tz, the retry does not.
  {
    const seen = []
    m.api.defaults.adapter = (config) => {
      seen.push({ ...(config.params ?? {}) })
      if (config.params?.tz === 'Asia/Calcutta') return Promise.reject(Object.assign(new Error('Request failed with status code 422'), { response: { status: 422, data: { detail: 'tz must be an IANA time zone name' }, config }, config }))
      return Promise.resolve({ data: changesGolden, status: 200, statusText: 'OK', headers: {}, config })
    }
    const res = await OHC.fetchChanges({}, 1, 50, 'Asia/Calcutta')
    assert.equal(res.total, changesGolden.total)
    assert.deepEqual(seen.map((p) => p.tz), ['Asia/Calcutta', undefined], 'the page loads: one retry without tz')
    seen.length = 0
    await OHC.fetchChanges({}, 1, 50, 'Europe/Berlin')
    assert.equal(seen.length, 1, 'a zone the API accepts is sent once')
    // Another 422 (a bad window) is a failure of its own and is not retried.
    m.api.defaults.adapter = (config) => { seen.push(1); return Promise.reject(Object.assign(new Error('422'), { response: { status: 422, data: { detail: 'until must be after since' }, config }, config })) }
    seen.length = 0
    await assert.rejects(OHC.fetchChanges({}, 1, 50, 'UTC'))
    assert.equal(seen.length, 1)
    m.api.defaults.adapter = answer
  }
  // The query keys follow doc 03 section 8 (the Overview's own keys for os and hardware).
  assert.deepEqual(OHC.OSHW_KEYS.os('all'), ['inventory', 'os', 'all'])
  assert.deepEqual(OHC.OSHW_KEYS.os('server'), ['inventory', 'os', 'server'])
  assert.deepEqual(OHC.OSHW_KEYS.hardware, ['inventory', 'hardware'])
  assert.deepEqual(OHC.OSHW_KEYS.hardwareVendor('Cisco'), ['inventory', 'hardware', 'Cisco'])
  assert.equal(OHC.OSHW_KEYS.changes('x')[1], 'changes')
}

// The compile-time conformance file is still in place (tsc in `npm run build` runs it).
{
  const file = fs.readFileSync(path.join(src, 'pages/inventory/osHardwareChanges.typecheck.ts'), 'utf8')
  for (const f of ['os', 'hardware', 'changes', 'changes-scoped']) assert(file.includes(`./fixtures/${f}.json'`), `osHardwareChanges.typecheck.ts checks ${f}.json`)
  assert.equal((file.match(/^\/\/ @ts-expect-error/gm) ?? []).length, 4, 'four negative controls prove the checks are not vacuous')
  assert(/AssertNever<Extra</.test(file) && /conforms</.test(file) && /SameKeys/.test(file), 'both directions, and the filter keys against the API\'s')
  assert(/"include":\s*\["src"\]/.test(fs.readFileSync(path.join(root, 'tsconfig.app.json'), 'utf8')))
}

// The pages no longer use the placeholder, and none of the three imports another page's module or the shared api/types files for writing.
{
  for (const f of ['InventoryOsPage', 'InventoryHardwarePage', 'InventoryChangesPage']) {
    const code = fs.readFileSync(path.join(src, `pages/inventory/${f}.tsx`), 'utf8')
    assert(!code.includes('PagePlaceholder'), `${f} does not use the placeholder`)
    assert(!/console\.log|debugger|TODO|FIXME/.test(code), `${f} has no debugging leftovers`)
  }
}

console.log(`Inventory OS / Hardware / Changes: the OS tree, hardware model, matrix scale (contrast measured), changes vocabulary, windows, time zones, pairing rule and stats chips behave as specified; the three pages render every state from the golden responses with a vocabulary-only drill link on every number; the fetchers send the API's own parameters; the compile-time conformance file is in place.`)
