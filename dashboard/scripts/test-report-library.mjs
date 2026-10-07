// Report Library model (features/reports/library/model.ts): catalog → items,
// visibility, views, search, filters, sorting, grouping, URL state and links.
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

const root = process.cwd()
const out = await build({
  entryPoints: [path.join(root, 'src/features/reports/library/model.ts')],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'neutral',
  alias: { '@': path.join(root, 'src') },
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)

// ── a small catalog ────────────────────────────────────────────────────────
const sec = (id, title, extra = {}) => ({
  id, title, description: `${title} description`, category: 'Availability', scope: 'fleet',
  requires: ['devices.view'], options: [], semantics: 'window', available: true, ...extra,
})
const catalog = {
  categories: [
    { id: 'executive', title: 'Executive & SLA', icon: 'BarChart3', description: 'Leadership summaries.' },
    { id: 'availability', title: 'Availability & Uptime', icon: 'Activity', description: 'Uptime.' },
    { id: 'traffic', title: 'Traffic Analysis', icon: 'Waypoints', description: 'NetFlow.' },
    { id: 'servers', title: 'Servers & Agents', icon: 'Server', description: 'Servers.' },
  ],
  sections: [
    sec('kpi_summary', 'KPI summary'),
    sec('top_downtime', 'Top devices by downtime', {
      options: [
        { key: 'top_n', label: 'Top', type: 'int', default: 10, min: 5, max: 100 },
        { key: 'sort', label: 'Sort by', type: 'select', default: 'downtime', choices: [['downtime', 'Downtime'], ['outages', 'Outages']] },
        { key: 'hide_empty', label: 'Hide if empty', type: 'bool', default: false },
      ],
    }),
    sec('top_talkers', 'Top talkers', { requires: ['netflow.view'], scope: 'netflow', available: false }),
    sec('server_load', 'Server load', { requires: ['servers.view'], available: false }),
  ],
  types: [
    { key: 'executive_summary', title: 'Executive Summary', description: 'Legacy', category: 'legacy', engine: 'legacy', formats: ['pdf'] },
    {
      key: 'executive_overview', title: 'Executive Overview', description: 'Availability against target for leadership.',
      category: 'executive', engine: 'sections', formats: ['pdf', 'html', 'xlsx'],
      entries: [{ id: 'kpi_summary', title: null, available: true }, { id: 'top_downtime', title: 'Worst devices', options: { top_n: 5 }, available: true }, { id: 'top_talkers', available: false }],
      audience: ['Executive', 'IT manager'], tags: ['sla', 'board'], default_period: 'previous_month', frequency: 'monthly',
      icon: 'BarChart3', featured: true, filterable: ['devices'], requires: ['devices.view', 'netflow.view'], available: true,
    },
    {
      key: 'availability', title: 'Availability & SLA Report', description: 'Uptime for every node.',
      category: 'availability', engine: 'sections', formats: ['pdf', 'html', 'xlsx'],
      entries: [{ id: 'top_downtime', available: true }], audience: ['NOC operator'], tags: ['uptime'],
      default_period: 'last_7d', frequency: 'weekly', icon: 'Activity', featured: false, available: true,
    },
    {
      key: 'netflow_top', title: 'NetFlow Top Talkers', description: 'Busiest hosts.',
      category: 'traffic', engine: 'sections', formats: ['pdf'], entries: [{ id: 'top_talkers', available: false }],
      audience: ['Network engineer'], tags: [], default_period: 'last_24h', frequency: 'daily', featured: true, available: false,
    },
    {
      key: 'zz_uncategorised', title: 'Alpha Misc', description: 'Odd one out.', category: 'mystery', engine: 'sections',
      formats: ['pdf'], entries: [{ id: 'kpi_summary', available: true }], audience: [], tags: [], featured: false, available: true,
    },
  ],
  custom: [
    {
      id: 'c1', name: 'My SLA board pack', description: 'Monthly pack', sections: ['kpi_summary'], entries: [{ id: 'kpi_summary', title: null }],
      category: null, tags: ['board'], visibility: 'private', source_key: 'executive_overview', default_period: null,
      owner_id: 'u1', owner_name: 'Alice', is_owner: true, can_edit: true, can_delete: true, can_change_visibility: true,
      schedules: 2, created_at: '2026-10-01T08:00:00', updated_at: '2026-10-05T08:00:00',
    },
    {
      id: 'c2', name: 'Team traffic', description: '', sections: ['top_talkers'], entries: [{ id: 'top_talkers' }],
      category: 'traffic', tags: [], visibility: 'shared', source_key: null, default_period: 'last_7d',
      owner_id: 'u2', owner_name: 'Bob', is_owner: false, can_edit: false, can_delete: false, can_change_visibility: false,
      schedules: 0, created_at: null, updated_at: null,
    },
    {
      id: 'c3', name: 'Server weekly', description: 'Servers', sections: ['server_load'], entries: [{ id: 'server_load' }],
      category: 'servers', tags: [], visibility: 'shared', source_key: null, default_period: 'last_7d',
      owner_id: 'u2', owner_name: 'Bob', is_owner: false, can_edit: true, can_delete: true, can_change_visibility: false,
      schedules: 0, created_at: null, updated_at: '2026-09-01T00:00:00',
    },
    {
      id: 'c4', name: 'Shared outages', description: 'Outage digest', sections: ['top_downtime'], entries: [{ id: 'top_downtime', options: { top_n: 25, hide_empty: true } }],
      category: 'availability', tags: ['outage'], visibility: 'shared', source_key: 'availability', default_period: 'previous_week',
      owner_id: 'u1', owner_name: 'Alice', is_owner: true, can_edit: true, can_delete: true, can_change_visibility: true,
      schedules: 0, created_at: null, updated_at: null,
    },
  ],
  periods: [],
  can: { manage: true, export: true, share: true },
}

const items = m.buildLibraryItems(catalog)
const refs = items.map((i) => i.ref)

// ── visibility: legacy types never; unavailable templates hidden ───────────
assert(!refs.includes('executive_summary'), 'legacy engine types are not library cards')
assert(!refs.includes('netflow_top'), 'templates the role cannot see at all are hidden')
assert(!refs.includes('custom:c2'), 'custom templates with no visible section are hidden from readers')
assert(refs.includes('custom:c3'), 'an editor still sees (and can fix) a template with no visible sections')
assert.deepEqual(refs, ['executive_overview', 'availability', 'zz_uncategorised', 'custom:c1', 'custom:c3', 'custom:c4'])

const byRef = Object.fromEntries(items.map((i) => [i.ref, i]))
const exec = byRef.executive_overview
assert.equal(exec.partial, true, 'a hidden section marks the template partial')
assert.deepEqual(exec.sectionTitles, ['KPI summary', 'Worst devices', 'Top talkers'])
assert.equal(exec.categoryTitle, 'Executive & SLA')
assert.equal(byRef['custom:c3'].restricted, true)
assert.equal(byRef.zz_uncategorised.category, m.OTHER_CATEGORY, 'unknown categories fall under Other')

// Custom templates inherit presentation from the built-in they were made from.
const c1 = byRef['custom:c1']
assert.equal(c1.origin, 'custom')
assert.equal(c1.category, 'executive')
assert.deepEqual(c1.audience, ['Executive', 'IT manager'])
assert.equal(c1.defaultPeriod, 'previous_month')
assert.equal(c1.sourceTitle, 'Executive Overview')
assert.equal(c1.schedules, 2)
assert.deepEqual(c1.requires, ['devices.view'])

// ── views ───────────────────────────────────────────────────────────────────
const ctx = { favorites: ['custom:c4', 'availability', 'gone'], recent: [{ ref: 'availability', at: 100 }, { ref: 'custom:c1', at: 300 }, { ref: 'deleted', at: 400 }] }
const counts = m.viewCounts(items, ctx)
assert.deepEqual(counts, { all: 6, favorites: 2, recent: 2, mine: 2, shared: 2, builtin: 3 }, 'stale favourites and recents are not counted')

const run = (patch) => m.applyFilters(items, { ...m.DEFAULT_FILTERS, ...patch }, ctx)
assert.deepEqual(run({ view: 'mine' }).items.map((i) => i.ref), ['custom:c1', 'custom:c4'])
assert.deepEqual(run({ view: 'shared' }).items.map((i) => i.ref), ['custom:c3', 'custom:c4'])
assert.deepEqual(run({ view: 'favorites' }).items.map((i) => i.ref), ['custom:c4', 'availability'], 'favourites keep their starred order')
assert.deepEqual(run({ view: 'recent' }).items.map((i) => i.ref), ['custom:c1', 'availability'], 'recently viewed is newest first')
assert.equal(run({ view: 'delivered' }).items.length, 0)
assert.deepEqual(run({ category: 'availability' }).items.map((i) => i.ref), ['availability', 'custom:c4'])

// ── search: title, tags, section titles, description, owner; all tokens must match
assert.deepEqual(run({ q: 'worst' }).items.map((i) => i.ref), ['executive_overview'], 'section titles are searchable')
assert.deepEqual(run({ q: 'board' }).items.map((i) => i.ref), ['custom:c1', 'executive_overview'], 'tags are searchable; a title word ranks above a tag')
assert.deepEqual(run({ q: 'alice' }).items.map((i) => i.ref), ['custom:c1', 'custom:c4'], 'owners are searchable')
assert.deepEqual(run({ q: 'outage digest' }).items.map((i) => i.ref), ['custom:c4'])
assert.equal(run({ q: 'outage nonsense' }).items.length, 0, 'every token must match')
const sla = run({ q: 'sla' }).items.map((i) => i.ref)
// Title words outrank tags; ties keep catalog order; c4 matches only through the template it was based on.
assert.deepEqual(sla, ['availability', 'custom:c1', 'executive_overview', 'custom:c4'])
assert.equal(m.searchScore(exec, m.tokenize('  ')), 0, 'no tokens scores nothing')
assert.deepEqual(m.tokenize('  Top   Talkers '), ['top', 'talkers'])

// ── filters ─────────────────────────────────────────────────────────────────
assert.deepEqual(run({ audience: 'Executive' }).items.map((i) => i.ref), ['executive_overview', 'custom:c1'])
assert.deepEqual(run({ origin: 'builtin' }).items.map((i) => i.ref), ['executive_overview', 'availability', 'zz_uncategorised'])
const r = run({ category: 'availability', origin: 'custom' })
assert.deepEqual([r.items.length, r.scopeTotal], [1, 2], '"Showing X of Y" counts within the rail selection')
assert.equal(m.isFiltered(m.DEFAULT_FILTERS), false)
assert.equal(m.isFiltered({ ...m.DEFAULT_FILTERS, sort: 'name' }), false, 'sorting is not filtering')
assert.equal(m.isFiltered({ ...m.DEFAULT_FILTERS, q: 'x' }), true)

// ── sorting ─────────────────────────────────────────────────────────────────
assert.deepEqual(run({}).items.map((i) => i.ref)[0], 'executive_overview', 'featured first by default')
assert.deepEqual(run({ sort: 'name' }).items.map((i) => i.title), ['Alpha Misc', 'Availability & SLA Report', 'Executive Overview', 'My SLA board pack', 'Server weekly', 'Shared outages'])
assert.deepEqual(run({ sort: 'recent' }).items.map((i) => i.ref).slice(0, 2), ['custom:c1', 'availability'], 'recently used first, then by name')

// ── rail, groups and shelves ────────────────────────────────────────────────
const rail = m.railCategories(items, catalog.categories)
assert.deepEqual(rail.map((c) => [c.id, c.count]), [['executive', 2], ['availability', 2], ['traffic', 0], ['servers', 1], ['other', 1]])
assert.deepEqual(m.groupByCategory(items, catalog.categories).map((g) => g.category.id), ['executive', 'availability', 'servers', 'other'])
assert.deepEqual(m.featuredItems(items).map((i) => i.ref), ['executive_overview'])
assert.deepEqual(m.recentItems(items, ctx.recent).map((x) => x.item.ref), ['custom:c1', 'availability'])
assert.deepEqual(m.audienceOptions(items), ['Executive', 'IT manager', 'NOC operator'])

// ── URL state round-trips and rejects junk ─────────────────────────────────
const f = m.filtersFromParams(new URLSearchParams('view=all&category=traffic&q=top&audience=Executive&origin=custom&sort=name'))
assert.deepEqual(f, { view: 'all', category: 'traffic', q: 'top', audience: 'Executive', origin: 'custom', sort: 'name' })
assert.equal(m.filtersToParams(f).toString(), 'category=traffic&q=top&audience=Executive&origin=custom&sort=name')
assert.deepEqual(m.filtersFromParams(new URLSearchParams('view=bogus&origin=x&sort=y')), { ...m.DEFAULT_FILTERS })
assert.equal(m.filtersFromParams(new URLSearchParams('view=favorites&category=traffic')).category, null, 'a category only applies to All')
assert.equal(m.filtersToParams(m.DEFAULT_FILTERS).toString(), '')

// ── presentation ────────────────────────────────────────────────────────────
assert.equal(m.metaLine(exec), '3 sections · Previous month · Executive +1')
assert.equal(m.metaLine(byRef.zz_uncategorised), '1 section')
const topDowntime = catalog.sections[1]
assert.equal(m.optionSummary({ id: 'top_downtime', options: { top_n: 25, hide_empty: true } }, topDowntime), 'Top 25 · Sort by: Downtime · Hide if empty')
assert.equal(m.optionSummary({ id: 'top_downtime', options: { sort: 'outages' } }, topDowntime), 'Top 10 · Sort by: Outages', 'defaults fill unset options; false booleans are omitted')
assert.equal(m.optionSummary({ id: 'kpi_summary' }, catalog.sections[0]), '')
assert.equal(m.optionSummary({ id: 'x' }, undefined), '')
assert.equal(m.permissionLabel('netflow.view'), 'NetFlow traffic')
assert.equal(m.permissionLabel('custom.thing'), 'custom.thing')

// ── links and actions ───────────────────────────────────────────────────────
assert.equal(m.openLink(exec), '/reports/view/executive_overview')
assert.equal(m.openLink(c1), '/reports/view/custom?custom_id=c1')
assert.equal(m.customizeLink(exec), '/reports/builder?from=executive_overview')
assert.equal(m.customizeLink(c1), '/reports/builder?clone=c1')
assert.equal(m.editLink(c1), '/reports/builder?edit=c1')
assert.deepEqual(m.schedulePrefill(exec), { report_type: 'executive_overview', period: 'previous_month' })
assert.deepEqual(m.schedulePrefill(c1), { report_type: 'custom', custom_report_id: 'c1', period: 'previous_month' })
assert.deepEqual(m.schedulePrefill(byRef.zz_uncategorised), { report_type: 'zz_uncategorised', period: 'last_7d' })
assert.equal(m.conflictScheduleCount('3 schedule(s) deliver this template. Delete them too, or keep the template.'), 3)
assert.equal(m.conflictScheduleCount('Conflict'), null)

// ── delivered reports ───────────────────────────────────────────────────────
assert.equal(m.runTone('success'), 'success')
assert.equal(m.runTone('partial'), 'warning')
assert.equal(m.runTone('failed'), 'danger')
assert.equal(m.runTone(null), 'neutral')
const runs = [
  { id: '1', title: 'Executive Overview', report_type: 'executive_overview', period: 'previous_month', token: 't1', status: 'success', delivered_to: [], generated_at: null, schedule_name: 'Board pack' },
  { id: '2', title: 'Availability', report_type: 'availability', period: 'last_7d', token: null, status: 'failed', delivered_to: [], generated_at: null, schedule_name: null },
]
assert.deepEqual(m.filterRuns(runs, 'board').map((x) => x.id), ['1'])
assert.deepEqual(m.filterRuns(runs, 'failed last 7').map((x) => x.id), ['2'], 'period labels are searchable')
assert.equal(m.filterRuns(runs, '').length, 2)
assert.equal(m.sharedRunLink('a/b'), '/api/v1/reports/shared/a%2Fb')
assert.equal(m.sharedRunLink(null), null)

console.log('report library ok: catalog items and visibility, views, search, filters, sorting, rail counts, URL state, links and runs')
