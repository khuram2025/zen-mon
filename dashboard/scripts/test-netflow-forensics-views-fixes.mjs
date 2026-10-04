// Forensics / saved-views fixes from the NetFlow acceptance pass (wave C): section views keep their
// ranking side, a finding id alone never runs an "evidence" search, the previous search's count is
// never shown under new chips, exports carry the search snapshot, NSEL events are disclosed and
// excluded from card drill-downs, ICMP type/code are not ports, the crumb keeps the context, and the
// DSCP views list "Not exported" apart from Best Effort.
// Run from dashboard/: node scripts/test-netflow-forensics-views-fixes.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')

const entry = `
export * as M from '@/pages/netflow/forensics/model'
export * as F from '@/lib/netflow/filters'
export * as L from '@/lib/netflow/links'
export * as W from '@/lib/netflow/window'
export * as CM from '@/pages/netflow/cards/model'
export { EvidenceBanner } from '@/pages/netflow/forensics/EvidenceBanner'
export { ResultsTable, ResultsCards } from '@/pages/netflow/forensics/ResultsTable'
export { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
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
const noop = () => {}
const media = () => ({ matches: false, addEventListener: noop, removeEventListener: noop, addListener: noop, removeListener: noop })
globalThis.window ??= globalThis
for (const [k, v] of Object.entries({ addEventListener: noop, removeEventListener: noop, dispatchEvent: () => true, matchMedia: media })) {
  globalThis[k] ??= v
}
globalThis.localStorage ??= { getItem: () => null, setItem: noop, removeItem: noop, clear: noop, key: () => null, length: 0 }
globalThis.sessionStorage ??= globalThis.localStorage
globalThis.document ??= {
  documentElement: { classList: { add: noop, remove: noop, toggle: noop, contains: () => false }, setAttribute: noop, style: {} },
  addEventListener: noop,
  removeEventListener: noop,
  querySelector: () => null,
  createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop }),
  body: { appendChild: noop, removeChild: noop },
  cookie: '',
}
globalThis.location ??= { href: 'http://localhost/', pathname: '/', search: '', hash: '', origin: 'http://localhost' }
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { M, F, L, W, CM } = m
const h = m.createElement
const sp = (q) => new URLSearchParams(q)
const qsOf = (link) => new URL(link, 'http://x').searchParams
let n = 0
const t = (name, fn) => {
  try {
    fn()
  } catch (err) {
    console.error(`FAILED: ${name}`)
    throw err
  }
  n++
}
const fixedWindow = W.nfWindowFromRange({ key: 'custom', hours: 1, fromISO: '2026-10-01T02:00:00.000Z', toISO: '2026-10-01T03:00:00.000Z', isCustom: true, label: 'custom' }, 'UTC')

/* ─── FE-10: section views keep the ranking side ───────────────────── */

t('a top-receivers section view stores by=dst and reopens with it', () => {
  const params = M.sectionViewParams(['by', 'metric', 'resolve'], { by: 'dst', side: 'dst', resolve: false, sortKey: null, sortDir: 'desc' })
  assert.deepEqual(params, { by: 'dst', side: null, resolve: null, col: null, dir: null })
  const query = {
    ...F.buildSavedQuery({ kind: 'section', filter: { exporter: '198.18.211.1' }, window: { mode: 'fixed', from: '2026-10-01T02:00:00.000Z', to: '2026-10-01T03:00:00.000Z' }, sort: 'packets', params }),
    path: '/netflow/top-talkers',
  }
  assert.deepEqual(query.params, { by: 'dst' })
  // What the API stores comes back through migrateSavedQuery and opens with by=dst.
  const link = M.savedViewLink({ id: 'v1', kind: 'section', query })
  const p = qsOf(link)
  assert.ok(link.startsWith('/netflow/top-talkers?'), link)
  assert.equal(p.get('by'), 'dst')
  assert.equal(p.get('sort'), 'packets')
  assert.equal(p.get('exporter'), '198.18.211.1')
  assert.equal(p.get('view'), 'v1')
})

t('countries/ASN views keep side=src; tables keep their column sort', () => {
  const params = M.sectionViewParams(['side'], { by: 'total', side: 'src', resolve: false, sortKey: 'share', sortDir: 'asc' })
  assert.deepEqual(F.savedPageParams(params), { col: 'share', dir: 'asc', side: 'src' })
  const p = qsOf(L.savedQueryLink({ v: 2, kind: 'section', filter: {}, window: { mode: 'relative', range: '24h' }, params: { side: 'src', col: 'share', dir: 'asc' } }, '/netflow/countries'))
  assert.equal(p.get('side'), 'src')
  assert.equal(p.get('col'), 'share')
  assert.equal(p.get('dir'), 'asc')
})

t('stored page settings are validated on read (unknown keys and values dropped)', () => {
  const mg = F.migrateSavedQuery({ v: 2, kind: 'section', filter: {}, window: { mode: 'relative', range: '1h' }, params: { by: 'dst', evil: 'x', side: 'up', dir: 'asc', col: 'Bytes;' } })
  assert.deepEqual(mg.query.params, { by: 'dst', dir: 'asc' })
  assert.equal(F.migrateSavedQuery({ v: 2, kind: 'section', filter: {}, window: { mode: 'relative', range: '1h' } }).query.params, undefined)
  // v1 blobs never had page settings.
  assert.equal(F.migrateSavedQuery({ src: '10.0.0.1', by: 'dst' }).query.params, undefined)
})

t('the save dialog summary names the ranking side', () => {
  assert.equal(M.pageSettingsSummary({ by: 'dst' }, 'packets'), 'Ranked by packets received')
  assert.equal(M.pageSettingsSummary({ by: 'src' }, null), 'Ranked by bytes sent')
  assert.equal(M.pageSettingsSummary({ side: 'src', col: 'share', dir: 'asc' }, null), 'Source side · Table sorted by share, ascending')
  assert.equal(M.pageSettingsSummary(undefined, null), null)
})

/* ─── Evidence links: a finding id alone runs nothing ──────────────── */

t('a bare finding id is not runnable; links with filters or run=1 are', () => {
  const bare = M.appliedSearchFromUrl(sp('finding=00000000-0000-4000-8000-000000000000&finding_kind=icmp_flood'))
  assert.equal(bare.findingId, '00000000-0000-4000-8000-000000000000')
  assert.equal(M.isRunnable(bare), false)
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('finding=icmp_flood&finding_id=f1'))), false)
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('finding=00000000-0000-4000-8000-000000000000&src=10.20.4.77&protocol=1&run=1'))), true)
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('run=1'))), true)
})

t('evidence state: 404/403 and malformed ids are "missing", other failures "unverified"', () => {
  const id = '00000000-0000-4000-8000-000000000000'
  assert.equal(M.evidenceState({ findingId: id, loading: false, found: false, status: 404 }), 'missing')
  assert.equal(M.evidenceState({ findingId: id, loading: false, found: false, status: 403 }), 'missing')
  assert.equal(M.evidenceState({ findingId: 'nope', loading: false, found: false, status: null }), 'missing')
  assert.equal(M.evidenceState({ findingId: id, loading: true, found: false, status: null }), 'loading')
  assert.equal(M.evidenceState({ findingId: id, loading: false, found: true, status: null }), 'found')
  assert.equal(M.evidenceState({ findingId: id, loading: false, found: false, status: 500 }), 'unverified')
  assert.equal(M.evidenceState({ findingId: null, loading: false, found: false, status: null }), 'unverified')
})

const renderBanner = (props, seed) => {
  // gcTime Infinity: no garbage-collection timer keeps node alive after the checks.
  const qc = new m.QueryClient({ defaultOptions: { queries: { retry: false, enabled: false, gcTime: Infinity } } })
  if (seed) qc.setQueryData(['netflow', 'finding', props.findingId], seed)
  const html = m.renderToStaticMarkup(
    h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.EvidenceBanner, { filter: {}, window: fixedWindow, broadenLink: () => '/x', ...props }))),
  )
  qc.clear()
  return html
}

t('banner: an unknown finding is reported, never shown as evidence', () => {
  const html = renderBanner({ finding: 'icmp_flood', findingId: 'not-a-finding-id', runnable: false })
  assert.match(html, /Finding not found or not visible/)
  assert.match(html, /Nothing was searched/)
  assert.doesNotMatch(html, /Evidence<\/span> for/)
  const withFilters = renderBanner({ finding: 'icmp_flood', findingId: 'bad', runnable: true, filter: { src: '10.20.4.77' } })
  assert.match(withFilters, /match the link’s filters only; they are not confirmed evidence/)
})

t('banner: a confirmed finding opened without filters offers its own evidence search', () => {
  const id = '11111111-1111-4111-8111-111111111111'
  const finding = {
    id, kind: 'icmp_flood', label: 'ICMP flood', persisted: true, severity: 'warning', status: 'open', host: '10.20.4.77',
    evidence: { window: { from: '2026-10-01T00:00:00Z', to: '2026-10-01T09:05:00Z' }, filter: { src: '10.20.4.77', protocol: 1, exporter: '127.0.0.61' } },
    first_seen: '2026-10-01T00:00:00Z', last_seen: '2026-10-01T09:05:00Z',
  }
  const html = renderBanner({ finding: null, findingId: id, runnable: false }, finding)
  assert.match(html, /Evidence<\/span> for ICMP flood/)
  assert.match(html, /This link carries no search conditions, so nothing was searched/)
  const href = html.match(/href="([^"]+)"[^>]*>(?:<svg[^]*?<\/svg>)?Search this finding/)?.[1]?.replace(/&amp;/g, '&')
  assert.ok(href, html)
  const p = qsOf(href)
  assert.equal(p.get('src'), '10.20.4.77')
  assert.equal(p.get('protocol'), '1')
  assert.equal(p.get('run'), '1')
  assert.equal(p.get('finding'), id)
})

/* ─── Placeholder / failed searches ────────────────────────────────── */

const page = (o = {}) => ({
  items: [], page_size: 500, has_more: false, next_cursor: null, total: 1090, total_exact: true, total_bytes: 0, total_packets: 0,
  offset: 0, sort: 'timestamp', order: 'desc', snapshot_at: '2026-10-01T09:47:27.000Z', snapshot_ms: 1790848047000,
  window: { from: '2026-10-01T08:47:00.000Z', to: '2026-10-01T09:47:00.000Z', mode: 'relative', tz: 'UTC', effective_from: null },
  unavailable: null, ...o,
})

t('heading: never the previous count while loading, "Search failed" after an error', () => {
  assert.equal(M.searchHeading({ page: page(), placeholder: true, failed: false, loaded: 500 }), 'Searching…')
  assert.equal(M.searchHeading({ page: undefined, placeholder: false, failed: true, loaded: 0 }), 'Search failed')
  assert.equal(M.searchHeading({ page: undefined, placeholder: false, failed: false, loaded: 0 }), 'Searching…')
  assert.equal(M.searchHeading({ page: page(), placeholder: false, failed: false, loaded: 500 }), 'Showing 1–500 of 1,090')
  assert.equal(M.searchHeading({ page: page({ unavailable: { reason: 'raw_retention', raw_days: 7, earliest_raw: null, message: 'x' } }), placeholder: false, failed: false, loaded: 0 }), 'Raw records unavailable for this period')
})

/* ─── Export pinned to the snapshot ────────────────────────────────── */

t('"All matching rows" export carries the window and the search snapshot', () => {
  const p = M.exportApiParams({ filter: { exporter: '127.0.0.61' }, sort: 'timestamp', order: 'desc', page: page(), windowParams: { range: '1h' }, tz: 'UTC' })
  assert.equal(p.get('snapshot'), '1790848047000')
  assert.equal(p.get('from'), '2026-10-01T08:47:00.000Z')
  assert.equal(p.get('to'), '2026-10-01T09:47:00.000Z')
  assert.equal(p.get('exporter'), '127.0.0.61')
  assert.ok(!p.has('page_size') && !p.has('range'))
  const iso = M.exportApiParams({ filter: {}, sort: 'bytes', order: 'asc', page: page({ snapshot_ms: undefined }), windowParams: {}, tz: 'UTC' })
  assert.equal(iso.get('snapshot'), '2026-10-01T09:47:27.000Z')
  const none = M.exportApiParams({ filter: {}, sort: 'timestamp', order: 'desc', page: null, windowParams: { range: '1h' }, tz: 'UTC' })
  assert.ok(!none.has('snapshot') && none.get('range') === '1h')
})

/* ─── NSEL create/deny events ──────────────────────────────────────── */

t('fw_event=flows is a filter value with a label, and round-trips', () => {
  assert.ok(F.FW_EVENTS.includes('flows'))
  assert.match(F.FW_EVENT_LABELS.flows, /Flow records only/)
  assert.equal(F.parseFlowFilterDetailed(sp('fw_event=flows')).filter.fw_event, 'flows')
})

t('Forensics counts the events and excludes them on request', () => {
  assert.equal(M.firewallEventCount(page({ fw_events: { created: 4514, denied: 4088, exact: true } })), 8602)
  assert.equal(M.firewallEventCount(page({ fw_events: null })), 0)
  const next = M.flowRecordsOnlyParams(sp('exporter=127.0.0.65&protocol=6&cursor=abc&finding=x'))
  assert.equal(next.get('fw_event'), 'flows')
  assert.equal(next.get('run'), '1')
  assert.ok(!next.has('cursor') && !next.has('finding'))
  assert.equal(next.get('protocol'), '6')
})

t('card drill-downs ask for flow records only unless the page chose an event', () => {
  const w = { key: '1h', mode: 'relative', hours: 1, from: '2026-10-01T08:00:00.000Z', to: '2026-10-01T09:00:00.000Z', tz: 'UTC', label: 'Last 1h' }
  const meta = { from: '2026-10-01T08:00:00.000Z', to: '2026-10-01T09:00:00.000Z' }
  assert.equal(qsOf(CM.forensicsLink({ exporter: '127.0.0.65' }, { protocol: 6 }, w, meta)).get('fw_event'), 'flows')
  assert.equal(qsOf(CM.forensicsLink({ exporter: '127.0.0.65', fw_event: 'denied' }, {}, w, meta)).get('fw_event'), 'denied')
})

/* ─── ICMP type/code ───────────────────────────────────────────────── */

const row = (o = {}) => ({
  id: 'a', timestamp: '2026-10-01T09:00:00.000Z', received_at: '2026-10-01T09:00:01.000Z', exporter: '127.0.0.61',
  observation_domain: 0, export_protocol: 'netflow9', flow_version: 9, ip_version: 4, src: '10.20.4.77', dst: '10.30.9.200',
  src_port: 0, dst_port: 2048, protocol: 1, protocol_name: 'ICMP', icmp_type: 8, icmp_code: 0, service: 'Echo request', service_port: null,
  application: null, class_direction: null, class_basis: null, tcp_flags: null, flags_state: 'n/a',
  tos: 0, dscp: 0, input_if: 3, output_if: 4, input_interface: null, output_interface: null,
  packets: 10, bytes: 1500, sampling_interval: 1, duration_ms: 250, src_as: null, dst_as: null, src_mask: null, dst_mask: null,
  next_hop: null, flow_direction: null, app_id: null, fw_event: null, nat: null, flow_end_reason: null, fields_present: 0,
  tracked: false, clock_clamped: false, reverse: false, ...o,
})

t('ICMP endpoints have no ":port"; type/code are named', () => {
  assert.equal(M.rowEndpoint(row(), 'src'), '10.20.4.77')
  assert.equal(M.rowEndpoint(row(), 'dst'), '10.30.9.200')
  assert.equal(M.icmpTypeCodeText(row()), 'type 8 code 0')
  assert.equal(M.icmpTypeCodeText(row({ protocol: 58, icmp_type: undefined, icmp_code: undefined, dst_port: (1 << 8) | 4 })), 'type 1 code 4')
  assert.equal(M.rowEndpoint(row({ protocol: 6, src_port: 51000, dst_port: 443 }), 'dst'), '10.30.9.200:443')
  assert.equal(M.icmpTypeCodeText(row({ protocol: 6 })), null)
})

t('rendered table, cards and detail never show the encoded type as a port', () => {
  const props = { rows: [row()], columns: M.DEFAULT_COLUMNS, sort: 'timestamp', order: 'desc', onSort: noop, expanded: new Set(['a']), onToggle: noop, tz: 'UTC', window: fixedWindow, filter: {} }
  const table = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.ResultsTable, props)))
  assert.doesNotMatch(table, /:2048/)
  assert.match(table, /title="ICMP type 8 code 0"/)
  assert.match(table, /ICMP type \/ code/)
  const cards = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.ResultsCards, props)))
  assert.doesNotMatch(cards, /:2048|:0 /)
})

t('displayed-rows CSV: ports blank for ICMP, type/code columns', () => {
  const lines = M.displayedRowsCsv([row(), row({ id: 'b', protocol: 6, protocol_name: 'TCP', icmp_type: null, icmp_code: null, src_port: 51000, dst_port: 443 })], ['src', 'dst', 'protocol']).trimEnd().split('\r\n')
  assert.equal(lines[0], 'src,src_port,dst,dst_port,protocol,protocol_name,icmp_type,icmp_code')
  assert.equal(lines[1], '10.20.4.77,,10.30.9.200,,1,ICMP,8,0')
  assert.equal(lines[2], '10.20.4.77,51000,10.30.9.200,443,6,TCP,,')
})

/* ─── Header crumb keeps the investigation context ─────────────────── */

t('the NetFlow crumb opens the overview with the filter and window; exporter is a parent', () => {
  const links = M.forensicsHeaderLinks({ exporter: '198.18.211.1', talker: '10.77.1.1' }, fixedWindow)
  const p = qsOf(links.root)
  assert.ok(links.root.startsWith('/netflow?'), links.root)
  assert.equal(p.get('exporter'), '198.18.211.1')
  assert.equal(p.get('talker'), '10.77.1.1')
  assert.equal(p.get('range'), 'custom')
  assert.equal(p.get('from'), '2026-10-01T02:00:00.000Z')
  assert.equal(links.parents.length, 1)
  assert.ok(links.parents[0].to.startsWith('/netflow/devices/198.18.211.1?'))
  assert.equal(qsOf(links.parents[0].to).get('talker'), '10.77.1.1')
  assert.ok(!qsOf(links.parents[0].to).has('exporter'))
  assert.deepEqual(M.forensicsHeaderLinks({}, fixedWindow).parents, [])
  const html = m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.NetflowPageHeader, { title: 'Flow Forensics', crumb: 'Forensics', rootTo: links.root, parents: links.parents })))
  assert.ok(html.includes(`href="${links.root.replace(/&/g, '&amp;')}"`), html)
  // Other pages keep the plain root.
  assert.match(m.renderToStaticMarkup(h(m.MemoryRouter, null, h(m.NetflowPageHeader, { title: 'X' }))), /href="\/netflow"/)
})

/* ─── DSCP: Not exported is its own row ────────────────────────────── */

t('DSCP rows list "Not exported" by size and never fold it into other classes', () => {
  const items = [{ dscp: 0, label: 'BE', bytes: 800, packets: 1, flows: 1, selected: false }, { dscp: 46, label: 'EF', bytes: 200, packets: 1, flows: 1, selected: false }]
  const out = CM.dscpShareItems({ items, not_exported: { bytes: 1000, packets: 5, flows: 1 } })
  assert.deepEqual(out.map((d) => (d.notExported ? 'ne' : d.dscp)), ['ne', 0, 46])
  const small = CM.dscpShareItems({ items, not_exported: { bytes: 1, packets: 1, flows: 1 } }, 2)
  assert.deepEqual(small.map((d) => (d.notExported ? 'ne' : d.dscp)), [0, 'ne', 46])
  assert.equal(CM.dscpShareItems({ items, not_exported: null }).length, 2)
  // Only not-exported traffic (an NSEL exporter): one row, nothing claimed as Best Effort.
  const nsel = CM.dscpShareItems({ items: [], not_exported: { bytes: 18_600_000_000, packets: 1, flows: 9854 } })
  assert.equal(nsel.length, 1)
  assert.ok(nsel[0].notExported && nsel[0].dscp === -1)
})

console.log(`test-netflow-forensics-views-fixes: ${n} checks passed`)
