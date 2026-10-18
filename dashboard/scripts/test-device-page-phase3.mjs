// Device page Phase 3: template-declared vendor tabs, server-saved layouts, the Related panel model,
// previous-period comparison and PNG layout, and alert titles named from the triggering condition.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

async function load(path, replace = {}) {
  let src = fs.readFileSync(path, 'utf8')
  for (const [from, to] of Object.entries(replace)) src = src.replace(from, to)
  const compiled = await transformWithEsbuild(src, path.split('/').pop(), { loader: 'ts', format: 'esm' })
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
}

/* ── Vendor tabs ─────────────────────────────────────────────────────────── */
const tabs = await load('src/pages/device/tabs.ts')
const fg = { id: 'd', hostname: 'fg', vendor: 'Fortinet', snmp_enabled: true, profile_name: 'Fortinet FortiGate' }
const ids = (list) => list.map((t) => t.id)

assert.equal(tabs.templateTabId('VPN & SD-WAN'), 'tpl-vpn-sd-wan', 'same slug as device_insights.tab_id')
assert.equal(tabs.templateTabId('Pools & virtual servers'), 'tpl-pools-virtual-servers')
assert.equal(tabs.templateTabId('***'), 'tpl-tab')

// Before the summary answers: the default vendor tab only (old links keep working).
assert.deepEqual(ids(tabs.deviceTabs(fg, 'Fortinet')), ['summary', 'performance', 'interfaces', 'metrics', 'hardware', 'events', 'configuration'])
assert.equal(tabs.deviceTabs(fg, 'Fortinet').find((t) => t.id === 'metrics').label, 'Fortinet metrics')

const declared = [{ id: 'tpl-vpn-sd-wan', label: 'VPN & SD-WAN', groups: ['ipsec', 'sdwan'] }, { id: 'tpl-wireless', label: 'Wireless', groups: ['access_points'] }]
const groups = [{ key: 'system', kind: 'scalar', tab: null }, { key: 'ipsec', kind: 'table', tab: 'VPN & SD-WAN' },
  { key: 'sdwan', kind: 'table', tab: 'VPN & SD-WAN' }, { key: 'access_points', kind: 'table', tab: 'Wireless' }, { key: 'sensors', kind: 'table', tab: null }]
const withTabs = tabs.deviceTabs(fg, { vendor: 'Fortinet', tabs: declared, groups })
assert.deepEqual(ids(withTabs), ['summary', 'performance', 'interfaces', 'metrics', 'tpl-vpn-sd-wan', 'tpl-wireless', 'hardware', 'events', 'configuration'],
  'declared tabs sit between the default vendor tab and Hardware, in template order')
assert.deepEqual(withTabs.find((t) => t.id === 'tpl-vpn-sd-wan').groups, ['ipsec', 'sdwan'])
// Every non-hardware group in a declared tab: the default tab is left out.
const covered = tabs.deviceTabs(fg, { vendor: 'Fortinet', tabs: declared, groups: groups.filter((g) => g.key !== 'system') })
assert.ok(!ids(covered).includes('metrics'))
// Without SNMP or a template there are no vendor tabs at all.
assert.deepEqual(ids(tabs.deviceTabs({ ...fg, snmp_enabled: false }, { tabs: declared, groups })), ['summary', 'performance', 'events', 'configuration'])
assert.deepEqual(ids(tabs.deviceTabs({ ...fg, profile_name: null }, { tabs: declared, groups })), ['summary', 'performance', 'interfaces', 'hardware', 'events', 'configuration'])

// Group placement: declared tab, then Hardware, then the default vendor tab.
assert.equal(tabs.placeGroup({ key: 'ipsec', tab: 'VPN & SD-WAN' }), 'tpl-vpn-sd-wan')
assert.equal(tabs.placeGroup({ key: 'sensors' }), 'hardware')
assert.equal(tabs.placeGroup({ key: 'memory', kind: 'table' }), 'hardware')
assert.equal(tabs.placeGroup({ key: 'memory', kind: 'scalar' }), 'metrics')
assert.equal(tabs.placeGroup({ key: 'fans', tab: 'Chassis' }), 'tpl-chassis', 'a template may move even a hardware group')
// Issues open the tab that shows their group; a hidden tab falls back to the vendor tab, then Summary.
const groupOf = (key) => groups.find((g) => g.key === key)
assert.equal(tabs.issueTab({ tab: 'metrics', group: 'ipsec' }, withTabs, groupOf), 'tpl-vpn-sd-wan')
assert.equal(tabs.issueTab({ tab: 'metrics', group: 'sensors' }, withTabs, groupOf), 'hardware')
assert.equal(tabs.issueTab({ tab: 'metrics', group: 'system' }, withTabs, groupOf), 'metrics')
assert.equal(tabs.issueTab({ tab: 'metrics', group: 'ipsec' }, tabs.deviceTabs(fg, 'Fortinet'), groupOf), 'metrics')
assert.equal(tabs.issueTab({ tab: 'interfaces' }, withTabs), 'interfaces')
assert.equal(tabs.resolveTab('tpl-wireless', withTabs), 'tpl-wireless')
assert.equal(tabs.resolveTab('tpl-gone', withTabs), 'summary')
assert.equal(tabs.resolveTab('overview', withTabs), 'summary')

const page = fs.readFileSync('src/pages/DeviceDetailPage.tsx', 'utf8')
assert.match(page, /templateTabs\.map\(\(t\) => \(\s*<TabsContent key=\{t\.id\} value=\{t\.id\}>/, 'each declared tab renders its own content')
assert.match(page, /g\.tab === tab/, 'a declared tab shows exactly its groups')
assert.match(page, /placeGroup\(g\) === 'metrics'/, 'the default vendor tab keeps the undeclared non-hardware groups')
assert.match(fs.readFileSync('src/pages/device/HardwareTab.tsx', 'utf8'), /placeGroup\(g\) === 'hardware'/)
console.log('Vendor tabs passed: slugs, order, default-tab coverage, group placement, issue routing and URL resolution.')

/* ── Server-saved layouts ───────────────────────────────────────────────── */
const lay = await load('src/components/devices/widgetLayout.ts')
const items = [{ id: 'cpu', x: 0, y: 0, w: 6, h: 4 }]
const other = [{ id: 'cpu', x: 6, y: 0, w: 6, h: 4 }]
assert.deepEqual(lay.readStoredLayout(JSON.stringify({ version: 1, items })), { items, synced: false }, 'pre-server browser layouts read as unsynced')
assert.deepEqual(lay.readStoredLayout(lay.storedLayoutValue(items, true)), { items, synced: true })
assert.deepEqual(lay.readWidgetLayout(lay.storedLayoutValue(items, true)), items, 'the synced marker keeps the v1 format readable')
assert.equal(lay.readStoredLayout('nope'), null)
assert.deepEqual(lay.layoutTemplateIdentity('  PALO   ALTO NETWORKS ', ' Firewall '), { vendor: 'palo alto networks', device_type: 'firewall' })
assert.equal(lay.layoutTemplateIdentity('unknown', 'firewall'), null)
// Not answered yet (or unavailable): the browser copy shows as it is.
assert.deepEqual(lay.reconcileLayout({ items, synced: false }, undefined), { items, local: 'keep', upload: false })
// The server copy wins and is cached.
assert.deepEqual(lay.reconcileLayout({ items: other, synced: true }, items), { items, local: 'write', upload: false })
assert.deepEqual(lay.reconcileLayout(null, []), { items: [], local: 'write', upload: false }, 'an empty server layout is the standard-layout override')
// A browser-only layout is uploaded once; a synced one the server lost was reset elsewhere.
assert.deepEqual(lay.reconcileLayout({ items, synced: false }, null), { items, local: 'keep', upload: true })
assert.deepEqual(lay.reconcileLayout({ items, synced: true }, null), { items: null, local: 'remove', upload: false })
assert.deepEqual(lay.reconcileLayout(null, null), { items: null, local: 'keep', upload: false })
// The device layout still wins over the vendor/type template.
assert.equal(lay.effectiveWidgetLayout(items, other), items)
const grid = fs.readFileSync('src/components/devices/DeviceWidgetGrid.tsx', 'utf8')
assert.match(grid, /\/devices\/\$\{deviceId\}\/widget-layouts/, 'layouts are read from the server')
assert.match(grid, /api\.put\(url, \{ scope: slot/, 'layouts are saved to the server')
assert.match(grid, /persist\('device', null\)/, 'choosing the template forgets the device layout on the server')
console.log('Server-saved layouts passed: synced marker, template identity, reconcile rules and device-over-template precedence.')

/* ── Related ───────────────────────────────────────────────────────────── */
const rel = await load('src/pages/device/relatedModel.ts')
assert.equal(rel.offsetText(40), '40 s after')
assert.equal(rel.offsetText(-70), '1 min 10 s before')
assert.equal(rel.offsetText(-120), '2 min before')
assert.equal(rel.offsetText(0), 'at the same moment')
assert.equal(rel.sameTimeText({ at: 'x', old_status: 'up', new_status: 'down', offset_s: 40, anchor: { at: 'y', old_status: 'up', new_status: 'degraded' } }),
  'Down 40 s after this device went Degraded')
assert.equal(rel.childrenText({ total: 12, by_status: { up: 8, down: 3, degraded: 1 }, items: [] }), '12 managed · 3 down · 1 degraded')
assert.equal(rel.mapFocusHref('m 1', 'd'), '/maps/manual?map=m%201&mode=live&focus=d')
const empty = { parent: null, children: { total: 0, by_status: {}, items: [] }, dependencies: { upstream: [], downstream: [] }, maps: null,
  same_time: { window_s: 120, transitions: [], devices: [] }, unavailable: [] }
assert.ok(rel.relatedIsEmpty(empty))
assert.ok(!rel.relatedIsEmpty({ ...empty, maps: { maps: [{ id: 'm', name: 'Core' }], neighbours: [] } }))
assert.match(fs.readFileSync('src/pages/device/SummaryTab.tsx', 'utf8'), /<RelatedPanel deviceId=\{id\} \/>/)
const canvas = fs.readFileSync('src/features/maps/canvas/MapCanvas.tsx', 'utf8')
assert.match(canvas, /n\.device_id === focusDeviceId/, 'the map centres on ?focus=<device>')
assert.match(fs.readFileSync('src/features/maps/MapEditorV2.tsx', 'utf8'), /focusDeviceId=\{params\.get\('focus'\)\}/)
console.log('Related passed: offsets, same-time wording, managed counts, map focus links and the empty state.')

/* ── Performance: previous period and PNG ──────────────────────────────── */
const perf = await load('src/pages/device/performanceModel.ts')
assert.deepEqual(perf.previousWindow(1000, 4600), { fromTs: -2600, toTs: 1000, shiftMs: 3600 })
assert.deepEqual(perf.shiftPoints([{ ts: 0, value: 1 }, { ts: 10, value: null }], 100), [{ ts: 100, value: 1 }, { ts: 110, value: null }])
const pct = (v) => `${Math.round(v)}%`
assert.deepEqual(perf.periodDelta([40, 50], [30, 40], pct), { current: 45, previous: 35, delta: 10, text: '+10%', direction: 'up' })
assert.equal(perf.periodDelta([30], [40], pct).text, '−10%')
assert.equal(perf.periodDelta([40.2], [40], pct).direction, 'flat', 'a change below the shown precision is no change')
const none = perf.periodDelta([null, null], [40], pct)
assert.equal(none.delta, null, 'missing samples are unknown, never a zero change')
assert.equal(none.direction, 'unknown')
const layout = perf.pngLayout([{ width: 700, height: 200, full: true }, { width: 600, height: 170 }, { width: 600, height: 170 }, { width: 600, height: 170 }])
assert.equal(layout.width, 24 + 2 * (720 + 24))
assert.equal(layout.cells[0].w, 2 * 720 + 24, 'the pinned chart spans the row')
assert.deepEqual([layout.cells[1].x, layout.cells[2].x, layout.cells[3].x], [24, 24 + 744, 24])
assert.ok(layout.cells[1].y > layout.cells[0].y + layout.cells[0].h && layout.cells[3].y > layout.cells[1].y)
assert.ok(layout.height >= layout.cells[3].y + layout.cells[3].h)
assert.equal(perf.exportFilename('FortiGate 101F', Date.parse('2026-10-18T13:05:00Z'), 'png'), 'FortiGate_101F-performance-202610181305.png')
const perfTab = fs.readFileSync('src/pages/device/PerformanceTab.tsx', 'utf8')
assert.match(perfTab, /compare=previous|'compare', 'previous'/, 'the comparison is in the URL')
assert.match(perfTab, /Export PNG/)
assert.match(perfTab, /useSnmpMetricsBefore\(id, hours, hours, snmp && compare\)/, 'previous SNMP data is fetched only while comparing')
assert.match(fs.readFileSync('src/pages/device/hooks.ts', 'utf8'), /offset_hours=\$\{offsetHours\}/)
console.log('Performance passed: previous window, shift, unit deltas with unknowns, PNG grid layout and file names.')

/* ── Alert titles and owner/notes ──────────────────────────────────────── */
const ev = await load('src/pages/device/eventsModel.ts')
assert.equal(ev.deviceAlertTitle('Packet loss 33% (limit 10%) — fw (10.0.0.1)', 'Packet loss 33% (limit 10%)', 'fw', '10.0.0.1'), 'Packet loss 33% (limit 10%)')
assert.equal(ev.deviceAlertTitle('No response — fw (10.0.0.1)', null, 'fw', '10.0.0.1'), 'No response')
assert.equal(ev.deviceAlertTitle('High Latency: fw (10.0.0.1) is DEGRADED', null, 'fw', '10.0.0.1'), 'High Latency · Degraded')
assert.equal(ev.deviceAlertTitle('Device Down: fw (10.0.0.1) recovered — UP', null, 'fw', '10.0.0.1'), 'Device Down · Recovered')
assert.equal(ev.deviceAlertTitle('[ZenPlus CRITICAL] Tunnel rule fired', null, 'fw', '10.0.0.1'), 'Tunnel rule fired')
assert.equal(ev.deviceAlertTitle('High Latency: other (10.0.0.9) is DOWN', null, 'fw', '10.0.0.1'), 'High Latency: other (10.0.0.9) is DOWN')
const [titled] = ev.alertEvents([{ id: 'a', status: 'active', severity: 'warning', message: 'Packet loss 33% (limit 10%) — fw (10.0.0.1)',
  rule_name: 'High Latency', triggered_at: '2026-10-18T10:00:00Z', metadata: { condition: 'Packet loss 33% (limit 10%)' } }], { hostname: 'fw', ip_address: '10.0.0.1' })
assert.equal(titled.title, 'Packet loss 33% (limit 10%)')
assert.equal(titled.subtitle, 'Rule: High Latency', 'the rule stays visible beside the condition')
const config = fs.readFileSync('src/pages/device/ConfigurationTab.tsx', 'utf8')
assert.match(config, /<OwnerNotesCard device=\{device\} \/>/)
const notes = fs.readFileSync('src/pages/device/OwnerNotesCard.tsx', 'utf8')
assert.match(notes, /useCan\(\)\('devices\.manage'\)/, 'only devices.manage edits owner and notes')
assert.match(notes, /api\.put\(`\/devices\/\$\{device\.id\}`, \{ owner, notes \}\)/)
console.log('Alert titles and owner/notes passed: condition first, legacy titles shortened, rule kept, edit gated by devices.manage.')
