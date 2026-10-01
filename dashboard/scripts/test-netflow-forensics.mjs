// NetFlow Forensics and Saved Views page logic (NF-08, NF-09, NF-10, NF-11, NF-12, NF-34, FE-04, FE-10).
// Run from dashboard/: node scripts/test-netflow-forensics.mjs
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
export { ADVANCED_KEYS } from '@/pages/netflow/forensics/FilterBuilder'
export { SaveCurrentViewButton } from '@/pages/netflow/SaveCurrentViewButton'
export { NetflowForensicsPage } from '@/pages/netflow/ForensicsPage'
export { NetflowSavedViewsPage } from '@/pages/netflow/SavedViewsPage'
export { ResultsTable, ResultsCards } from '@/pages/netflow/forensics/ResultsTable'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
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
// The page modules (and the stores they import) touch `window`, `document` and `localStorage` at load
// time; give the bundle minimal inert browser globals.
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
const { M, F, L, W } = m
const sp = (q) => new URLSearchParams(q)
let n = 0
const t = (name, fn) => {
  fn()
  n++
}

/* ─── Applied search from the URL (NF-08, NF-11) ───────────────────── */
t('every filter key in a link is applied; aliases and zero values kept', () => {
  const a = M.appliedSearchFromUrl(sp('range=24h&talker=10.0.0.0/24&proto=6&dscp=0&iface=3@192.0.2.1&tcpflag=rst&hour=0&dow=2&app=DNS&netclass=Public&not_dst=2001:db8::/32&fw_event=denied&sort=bytes&order=asc&columns=time,src,bytes&finding=icmp_flood&finding_id=f1'))
  assert.deepEqual(a.filter, {
    iface: '3@192.0.2.1', talker: '10.0.0.0/24', not_dst: '2001:db8::/32', protocol: 6, app: 'DNS', dscp: 0,
    netclass: 'Public', tcpflag: 'rst', hour: 0, dow: 2, fw_event: 'denied',
  })
  assert.equal(a.sort, 'bytes')
  assert.equal(a.order, 'asc')
  assert.deepEqual(a.columns, ['time', 'src', 'bytes'])
  assert.equal(a.finding, 'icmp_flood')
  assert.equal(a.findingId, 'f1')
  assert.equal(M.isRunnable(a), true)
  // A cross-field conflict in a link (TCP flag on a non-TCP protocol) blocks the search.
  const conflict = M.appliedSearchFromUrl(sp('proto=89&tcpflag=rst'))
  assert.deepEqual(M.invalidKeys(conflict), ['tcpflag'])
  assert.equal(M.isRunnable(conflict), false)
})

t('runnable rules: filters, run=1, evidence; invalid values block the search', () => {
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('range=1h'))), false)
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('dscp=0'))), true) // numeric zero is a filter
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('min_bytes=0'))), true)
  assert.equal(M.isRunnable(M.appliedSearchFromUrl(sp('run=1'))), true)
  const bad = M.appliedSearchFromUrl(sp('src=invalid-ip&protocol=6'))
  assert.deepEqual(M.invalidKeys(bad), ['src'])
  assert.equal(M.isRunnable(bad), false)
  assert.equal(M.draftFromApplied(bad).src, 'invalid-ip') // shown as typed so it can be fixed
})

t('unknown sort/order/columns fall back to safe defaults', () => {
  const a = M.appliedSearchFromUrl(sp('sort=password&order=sideways&columns=nope'))
  assert.equal(a.sort, 'timestamp')
  assert.equal(a.order, 'desc')
  assert.deepEqual(a.columns, M.DEFAULT_COLUMNS)
})

/* ─── Draft (NF-11) ────────────────────────────────────────────────── */
t('draft validation mirrors the URL rules with inline messages', () => {
  const d = M.draftFromApplied({ filter: {}, invalid: {} })
  const r1 = M.parseDraft({ ...d, talker: 'invalid-ip', src_port: 'a', dscp: '64', dst_ports: '22,x' })
  assert.equal(r1.ok, false)
  for (const k of ['talker', 'src_port', 'dscp', 'dst_ports']) assert.ok(r1.errors[k], k)
  const r2 = M.parseDraft({ ...d, protocol: 'OSPF', talker: ' 2001:DB8::/32 ', iface_dir: 'any', dscp: '0' })
  assert.equal(r2.ok, true)
  assert.deepEqual(r2.filter, { talker: '2001:db8::/32', protocol: 89, dscp: 0 })
  assert.equal(M.parseDraft({ ...d, protocol: 'TCP (6)' }).filter.protocol, 6)
  assert.equal(M.parseDraft({ ...d, iface_dir: 'in' }).ok, false) // direction needs an interface
  assert.equal(M.parseDraft({ ...d, tcpflag: 'rst', protocol: '17' }).ok, false)
  assert.equal(M.draftsEqual(d, { ...d, talker: '  ' }), true)
  assert.equal(M.draftsEqual(d, { ...d, talker: '10.0.0.1' }), false)
})

t('submit writes canonical keys, run=1, drops cursor and (by default) evidence', () => {
  const next = M.submitSearchParams(sp('range=24h&proto=6&cursor=abc&finding=x&finding_id=y&sort=bytes'), { talker: '10.0.0.5', dscp: 0 })
  assert.equal(next.toString(), 'range=24h&sort=bytes&talker=10.0.0.5&dscp=0&run=1')
  const kept = M.submitSearchParams(sp('finding=x&finding_id=y'), { src: '10.0.0.5' }, { keepEvidence: true })
  assert.equal(kept.get('finding'), 'x')
})

t('an unchanged submit is recognised whatever the parameter order (refresh, no history entry)', () => {
  const cur = sp('range=24h&talker=10.20.4.13&protocol=17&run=1')
  const next = M.submitSearchParams(cur, { talker: '10.20.4.13', protocol: 17 })
  assert.equal(M.sameSearchParams(next, cur), true)
  assert.equal(M.sameSearchParams(M.submitSearchParams(cur, { talker: '10.20.4.14', protocol: 17 }), cur), false)
})

t('reset clears filters, sort, run, evidence and view but keeps window, tz and columns', () => {
  const next = M.resetSearchParams(sp('range=custom&from=2026-09-01T00:00:00.000Z&to=2026-09-02T00:00:00.000Z&tz=Asia/Riyadh&src=10.0.0.1&proto=6&dscp_eq=0&sort=bytes&order=asc&run=1&finding=a&finding_id=b&view=v&columns=time,src'))
  assert.deepEqual(Object.fromEntries(next), {
    range: 'custom', from: '2026-09-01T00:00:00.000Z', to: '2026-09-02T00:00:00.000Z', tz: 'Asia/Riyadh', columns: 'time,src',
  })
})

t('sort headers: same column flips, new column starts descending; default removes params', () => {
  assert.deepEqual(M.nextSort({ sort: 'bytes', order: 'desc' }, 'bytes'), { sort: 'bytes', order: 'asc' })
  assert.deepEqual(M.nextSort({ sort: 'bytes', order: 'asc' }, 'packets'), { sort: 'packets', order: 'desc' })
  assert.equal(M.sortSearchParams(sp('sort=bytes&order=asc&cursor=x&talker=1.1.1.1'), 'timestamp', 'desc').toString(), 'talker=1.1.1.1')
  assert.equal(M.sortSearchParams(sp(''), 'duration', 'asc').toString(), 'sort=duration&order=asc')
})

t('column chooser writes only non-default choices, in canonical order', () => {
  assert.equal(M.columnsSearchParams(sp('a=1'), [...M.DEFAULT_COLUMNS]).toString(), 'a=1')
  assert.equal(M.columnsSearchParams(sp(''), ['bytes', 'time', 'nat']).get('columns'), 'time,bytes,nat')
  assert.equal(M.parseColumns('bytes,time').join(','), 'time,bytes')
  assert.equal(M.isDefaultColumns(M.DEFAULT_COLUMNS), true)
})

/* ─── Results (NF-10) ──────────────────────────────────────────────── */
t('showing label states the loaded range and the total or its lower bound', () => {
  assert.equal(M.showingLabel(500, 49212, true), 'Showing 1–500 of 49,212')
  assert.equal(M.showingLabel(1000, 120000, false), 'Showing 1–1,000 of at least 120,000')
  assert.equal(M.showingLabel(5, 5, true), 'Showing 1–5 of 5')
  assert.equal(M.showingLabel(0, 0, true), 'No records matched')
  assert.equal(M.showingLabel(500, null, false), 'Showing 1–500 (total unknown)')
})

t('TCP flags: absent is "not exported", legacy is flagged, zero is a real value', () => {
  assert.equal(M.flagsText({ protocol: 6, tcp_flags: null, flags_state: 'absent' }).text, 'n/e')
  assert.equal(M.flagsText({ protocol: 6, tcp_flags: 0, flags_state: 'exported' }).text, '·')
  assert.match(M.flagsText({ protocol: 6, tcp_flags: 0, flags_state: 'legacy' }).title, /before field tracking/)
  assert.equal(M.flagsText({ protocol: 6, tcp_flags: 0x12, flags_state: 'exported' }).text, 'AS')
  assert.equal(M.flagsText({ protocol: 17, tcp_flags: null, flags_state: 'n/a' }).text, '—')
})

const row = (o = {}) => ({
  id: 'a', timestamp: '2026-09-30T12:00:00.000Z', received_at: '2026-09-30T12:00:01.000Z', exporter: '192.0.2.1',
  observation_domain: 0, export_protocol: 'ipfix', flow_version: 10, ip_version: 4, src: '10.0.0.1', dst: '10.0.0.2',
  src_port: 5000, dst_port: 443, protocol: 6, protocol_name: 'TCP', service: 'HTTPS', service_port: 443,
  application: 'Web (HTTP/HTTPS)', class_direction: 'request', class_basis: 'dst_rule', tcp_flags: 0x12, flags_state: 'exported',
  tos: 0, dscp: 0, input_if: 3, output_if: 70000,
  input_interface: { exporter_ip: '192.0.2.1', ifindex: 3, display_name: '=HYPERLINK("x")' }, output_interface: null,
  packets: 10, bytes: 1500, sampling_interval: 1, duration_ms: 250, src_as: null, dst_as: null, src_mask: null, dst_mask: null,
  next_hop: null, flow_direction: null, app_id: null, fw_event: null, nat: null, flow_end_reason: null, fields_present: 0,
  tracked: false, clock_clamped: false, reverse: false, ...o,
})

t('displayed-rows CSV: chosen columns, formula-safe text, CRLF, one line per row', () => {
  const csv = M.displayedRowsCsv([row(), row({ id: 'b', service: '+cmd', src: '2001:db8::5' })], ['time', 'src', 'service', 'interfaces', 'bytes'])
  const lines = csv.trimEnd().split('\r\n')
  assert.equal(lines[0], 'timestamp_utc,src,src_port,service,application,input_if,input_interface,output_if,output_interface,bytes')
  assert.equal(lines.length, 3)
  assert.ok(lines[1].includes(`"'=HYPERLINK(""x"")"`), lines[1])
  assert.ok(lines[2].includes("'+cmd"), lines[2])
  assert.ok(lines[2].startsWith('2026-09-30T12:00:00.000Z,2001:db8::5,5000,'))
  assert.equal(M.csvCell(-5), '-5') // numbers are never prefixed
  assert.match(M.displayedRowsFilename(500, Date.UTC(2026, 8, 30, 12)), /^netflow-forensics-displayed-500-rows-20260930T120000Z\.csv$/)
})

t('export mode: stream under the cap, job above it, refused over the job limit or without permission', () => {
  const base = { allowed: true, exact: true, streamMax: 100000, jobMax: 2000000 }
  assert.equal(M.exportMode({ ...base, total: 5000 }), 'stream')
  assert.equal(M.exportMode({ ...base, total: 500000 }), 'job')
  assert.equal(M.exportMode({ ...base, total: 5000, exact: false }), 'job') // a lower bound never streams
  assert.equal(M.exportMode({ ...base, total: 3000000 }), 'too_large')
  assert.equal(M.exportMode({ ...base, allowed: false, total: 10 }), 'forbidden')
  assert.equal(M.exportMode({ ...base, total: null }), 'unknown_total')
  assert.equal(M.exportMode({ ...base, total: 0 }), 'none')
})

/* ─── Evidence (NF-09) ─────────────────────────────────────────────── */
t('evidence: detector label, host, broaden keeps the frozen window and exporter', () => {
  assert.equal(M.detectorLabel('icmp_flood'), 'ICMP flood')
  assert.equal(M.detectorLabel('weird_new_kind'), 'Weird new kind')
  assert.equal(M.evidenceHost({ src: '10.12.50.81', protocol: 1 }), '10.12.50.81')
  assert.equal(M.evidenceHost({ src: '10.0.0.0/8' }, '::ffff:10.1.1.1'), '10.1.1.1')
  const link = M.broadenToHostLink(sp('range=custom&from=A&to=B&src=10.12.50.81&protocol=1&finding=icmp_flood&finding_id=x&run=1'), '10.12.50.81', '192.0.2.1')
  const u = new URL(link, 'http://x')
  assert.equal(u.pathname, '/netflow/forensics')
  assert.equal(u.searchParams.get('talker'), '10.12.50.81')
  assert.equal(u.searchParams.get('exporter'), '192.0.2.1')
  assert.equal(u.searchParams.get('from'), 'A')
  assert.equal(u.searchParams.get('to'), 'B')
  assert.equal(u.searchParams.get('run'), '1')
  for (const k of ['src', 'protocol', 'finding', 'finding_id']) assert.equal(u.searchParams.has(k), false, k)
})

t('evidence links from Anomalies (finding=<id>&finding_kind=<kind>) and the older shape both work', () => {
  const id = '3f2a9c1e-1b2c-4d5e-8f90-a1b2c3d4e5f6'
  const a = M.appliedSearchFromUrl(sp(`range=custom&from=A&to=B&src=10.1.1.1&protocol=1&finding=${id}&finding_kind=icmp_flood&run=1`))
  assert.equal(a.finding, 'icmp_flood')
  assert.equal(a.findingId, id)
  const b = M.appliedSearchFromUrl(sp('src=10.1.1.1&finding=syn_scan&finding_id=abc'))
  assert.deepEqual([b.finding, b.findingId], ['syn_scan', 'abc'])
  const c = M.appliedSearchFromUrl(sp(`src=10.1.1.1&finding=${id}`))
  assert.deepEqual([c.finding, c.findingId], [null, id])
  // A changed search and the broaden link drop every evidence parameter.
  const next = M.submitSearchParams(sp(`finding=${id}&finding_kind=icmp_flood&src=10.1.1.1`), { src: '10.1.1.2' })
  for (const k of ['finding', 'finding_id', 'finding_kind']) assert.equal(next.has(k), false, k)
  const broad = new URL(M.broadenToHostLink(sp(`finding=${id}&finding_kind=icmp_flood`), '10.1.1.1'), 'http://x')
  assert.equal(broad.searchParams.has('finding_kind'), false)
})

/* ─── Saved views (NF-12, NF-34, FE-10) ───────────────────────────── */
const view = (o) => ({
  id: 'v1', name: 'View', description: null, kind: 'forensics', visibility: 'private', pinned: false,
  query: { v: 2, kind: 'forensics', filter: { dst_port: 443 }, window: { mode: 'relative', range: '7d' }, sort: 'bytes', order: 'asc' },
  query_version: 2, query_issues: [], legacy_time: false, owner_id: 'u1', owner_username: 'alice', is_owner: true,
  can_edit: true, can_delete: true, can_change_visibility: true, updated_by_username: 'alice', created_at: null, updated_at: null, ...o,
})

t('saved view links reproduce filter, window, sort, order, columns and carry the view id', () => {
  const u = new URL(M.savedViewLink(view({ query: { v: 2, kind: 'forensics', filter: { dst_port: 443, dscp: 0 }, window: { mode: 'relative', range: '7d' }, sort: 'bytes', order: 'asc', columns: ['time', 'bytes'] } })), 'http://x')
  assert.equal(u.pathname, '/netflow/forensics')
  assert.deepEqual(Object.fromEntries(u.searchParams), { range: '7d', dst_port: '443', dscp: '0', sort: 'bytes', order: 'asc', columns: 'time,bytes', run: '1', view: 'v1' })
  // A port-only / DSCP-0-only view still runs on open (run=1).
  const a = M.appliedSearchFromUrl(new URLSearchParams(u.search))
  assert.equal(M.isRunnable(a), true)
  assert.deepEqual(a.filter, { dst_port: 443, dscp: 0 })
  const fixed = new URL(M.savedViewLink(view({ query: { v: 2, kind: 'forensics', filter: {}, window: { mode: 'fixed', from: '2026-09-01T00:00:00.000Z', to: '2026-09-02T00:00:00.000Z' } } })), 'http://x')
  assert.equal(fixed.searchParams.get('range'), 'custom')
  assert.equal(fixed.searchParams.get('from'), '2026-09-01T00:00:00.000Z')
})

t('overview/section views reopen their page path; a bad path falls back', () => {
  const v = view({ kind: 'section', query: { v: 2, kind: 'section', filter: { talker: '10.0.0.5' }, window: { mode: 'relative', range: '24h' }, path: '/netflow/top-talkers' } })
  const u = new URL(M.savedViewLink(v), 'http://x')
  assert.equal(u.pathname, '/netflow/top-talkers')
  assert.equal(u.searchParams.has('run'), false)
  const evil = view({ kind: 'overview', query: { v: 2, kind: 'overview', filter: {}, window: { mode: 'relative', range: '1h' }, path: 'https://evil.example' } })
  assert.equal(new URL(M.savedViewLink(evil), 'http://x').pathname, '/netflow')
})

t('v1 (legacy) queries are upgraded: numeric strings, proto alias, legacy time flagged', () => {
  const { query, legacyTime } = M.savedViewQuery(view({ query_version: 1, query: { src: '', dst: '', proto: '17', dst_port: '443', dscp: '0', sort: 'bytes', order: 'asc' } }))
  assert.equal(legacyTime, true)
  assert.deepEqual(query.filter, { protocol: 17, dst_port: 443, dscp: 0 })
  assert.equal(query.sort, 'bytes')
})

t('grouping: pinned, mine, shared; search by name, description or owner; kind filter', () => {
  const vs = [
    view({ id: '1', name: 'VPN', pinned: true }),
    view({ id: '2', name: 'Mine', is_owner: true }),
    view({ id: '3', name: 'Team', is_owner: false, owner_username: 'bob', visibility: 'shared', description: 'branch uplinks' }),
    view({ id: '4', name: 'Overview', kind: 'overview', is_owner: false, owner_username: 'carol', visibility: 'shared' }),
  ]
  const g = M.groupSavedViews(vs)
  assert.deepEqual([g.pinned.map((v) => v.id), g.mine.map((v) => v.id), g.shared.map((v) => v.id)], [['1'], ['2'], ['3', '4']])
  assert.deepEqual(M.groupSavedViews(vs, 'uplink').shared.map((v) => v.id), ['3'])
  assert.deepEqual(M.groupSavedViews(vs, 'CAROL').shared.map((v) => v.id), ['4'])
  assert.deepEqual(M.groupSavedViews(vs, '', 'overview').shared.map((v) => v.id), ['4'])
})

t('another user\'s private view (administrators only) is its own group, never "shared" or "pinned"', () => {
  const vs = [
    view({ id: '1', name: 'Mine' }),
    view({ id: '2', name: 'Theirs', is_owner: false, visibility: 'private', pinned: true, owner_username: 'bob' }),
    view({ id: '3', name: 'Team', is_owner: false, visibility: 'shared' }),
  ]
  const g = M.groupSavedViews(vs)
  assert.deepEqual([g.pinned, g.mine, g.shared, g.othersPrivate].map((l) => l.map((v) => v.id)), [[], ['1'], ['3'], ['2']])
})

t('legacy_time from the API (v1 upgraded server-side) is honoured', () => {
  const v2 = { v: 2, kind: 'forensics', filter: { dst_port: 22 }, window: { mode: 'relative', range: '1h' } }
  assert.equal(M.savedViewQuery(view({ query: v2, query_version: 1, legacy_time: true })).legacyTime, true)
  assert.equal(M.savedViewQuery(view({ query: v2 })).legacyTime, false)
})

t('saved window and sort labels', () => {
  assert.equal(M.savedWindowLabel({ mode: 'relative', range: '7d' }, 'UTC'), 'Last 7d')
  assert.equal(M.savedWindowLabel({ mode: 'fixed', from: '2026-09-01T00:00:00Z', to: '2026-09-02T00:00:00Z' }, 'UTC'), '2026-09-01 00:00 → 2026-09-02 00:00 UTC')
  assert.equal(M.sortLabel({ sort: 'bytes', order: 'asc' }), 'Sorted by bytes, ascending')
  assert.equal(M.sortLabel({}), null)
})

t('a saved query built from the applied state round-trips through the link', () => {
  const w = W.nfWindowFromRange({ key: '24h', hours: 24, fromISO: '2026-09-29T12:00:00.000Z', toISO: '2026-09-30T12:00:00.000Z', isCustom: false, label: 'Last 24h' }, 'UTC')
  const q = F.buildSavedQuery({ kind: 'forensics', filter: { port: 0, dscp: 0 }, window: W.savedQueryWindow(w, 'relative'), sort: 'duration', order: 'desc' })
  const u = new URL(L.savedQueryLink(q), 'http://x')
  const a = M.appliedSearchFromUrl(new URLSearchParams(u.search))
  assert.deepEqual(a.filter, { port: 0, dscp: 0 })
  assert.equal(a.sort, 'duration')
  assert.equal(u.searchParams.get('range'), '24h')
  const fixed = F.buildSavedQuery({ kind: 'forensics', filter: {}, window: W.savedQueryWindow(w, 'fixed', { from: '2026-09-29T12:00:00Z', to: '2026-09-30T12:00:00Z' }) })
  assert.deepEqual(fixed.window, { mode: 'fixed', from: '2026-09-29T12:00:00.000Z', to: '2026-09-30T12:00:00.000Z' })
})

/* ─── Field coverage (FE-04) and components ────────────────────────── */
t('the filter builder offers every contract filter key except tz', () => {
  const primary = ['talker', 'src', 'dst', 'protocol', 'port', 'exporter']
  const offered = new Set([...primary, ...m.ADVANCED_KEYS])
  const missing = F.FLOW_FILTER_KEYS.filter((k) => k !== 'tz' && !offered.has(k))
  assert.deepEqual(missing, [])
})

t('pages and components are exported', () => {
  for (const k of ['NetflowForensicsPage', 'NetflowSavedViewsPage', 'SaveCurrentViewButton', 'ResultsTable']) {
    assert.equal(typeof m[k], 'function', k)
  }
})

t('results table renders sortable headers with aria-sort and named detail buttons', () => {
  const w = W.nfWindowFromRange({ key: '1h', hours: 1, fromISO: '2026-09-30T11:00:00.000Z', toISO: '2026-09-30T12:00:00.000Z', isCustom: false, label: 'Last 1h' }, 'UTC')
  const html = m.renderToStaticMarkup(
    m.createElement(m.MemoryRouter, null,
      m.createElement(m.ResultsTable, {
        rows: [row(), row({ id: 'b', src: '2001:db8::5', dst_port: null, duration_ms: null, flags_state: 'absent', tcp_flags: null })],
        columns: M.DEFAULT_COLUMNS, sort: 'bytes', order: 'asc', onSort: () => {}, expanded: new Set(['a']), onToggle: () => {},
        tz: 'UTC', window: w, filter: {},
      })),
  )
  assert.match(html, /aria-sort="ascending"/)
  assert.match(html, /aria-label="Hide details for 10\.0\.0\.1 to 10\.0\.0\.2/)
  assert.match(html, /\[2001:db8::5\]:5000/)
  assert.match(html, /Fields the exporter sent/) // expanded row rendered
  assert.match(html, /Flow end \(UTC\)/)
  assert.ok(html.includes('n/e'))
})

t('phone layout: one card per record with endpoints, volume and a sort menu (NF-30)', () => {
  const w = W.nfWindowFromRange({ key: '1h', hours: 1, fromISO: '2026-09-30T11:00:00.000Z', toISO: '2026-09-30T12:00:00.000Z', isCustom: false, label: 'Last 1h' }, 'UTC')
  const html = m.renderToStaticMarkup(
    m.createElement(m.MemoryRouter, null,
      m.createElement(m.ResultsCards, {
        rows: [row(), row({ id: 'b', src: '2001:db8::5', fw_event: 'denied' })],
        columns: [...M.DEFAULT_COLUMNS, 'fw_event'], sort: 'bytes', order: 'desc', onSort: () => {}, expanded: new Set(), onToggle: () => {},
        tz: 'UTC', window: w, filter: {},
      })),
  )
  assert.equal((html.match(/<li /g) ?? []).length, 2)
  assert.match(html, /aria-label="Show details for 2001:db8::5 to 10\.0\.0\.2/)
  assert.match(html, /10\.0\.0\.1:5000/)
  assert.match(html, /1\.50 kB/)
  assert.match(html, /<option value="bytes:desc" selected="">/)
  assert.match(html, /Firewall event: /) // chosen extra columns are listed on the card
})

console.log(`test-netflow-forensics: ${n} checks passed`)
