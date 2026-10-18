import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const compiled = await transformWithEsbuild(fs.readFileSync('src/pages/device/interfaceModel.ts', 'utf8'), 'interfaceModel.ts', { loader: 'ts', format: 'esm' })
const m = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

const iface = (over = {}) => ({
  id: 1, if_index: 1, if_name: 'Gi0/1', if_descr: 'GigabitEthernet0/1', if_alias: null, if_type: 6, if_speed: 1e9,
  configured_speed_bps: null, mac_address: null, admin_status: 'up', oper_status: 'up', monitored: true,
  last_seen: null, last_up_at: '2026-10-18T10:00:00Z', ever_up: true, ...over,
})

/* ── Status: the interface_breakdown rules ─────────────────────────────── */
const kind = (over) => m.classifyInterface(iface(over)).kind
assert.equal(kind({}), 'up')
assert.equal(kind({ oper_status: 'down' }), 'down')
assert.equal(m.classifyInterface(iface({ oper_status: 'down' })).tone, 'crit')
assert.equal(kind({ oper_status: 'lowerLayerDown' }), 'down')
assert.equal(kind({ oper_status: '2' }), 'down')
assert.equal(kind({ oper_status: 'down', admin_status: 'down' }), 'admin_down')
assert.equal(kind({ oper_status: 'down', admin_status: '2' }), 'admin_down')
assert.equal(kind({ oper_status: 'down', ever_up: false, last_up_at: null }), 'unused')
assert.equal(kind({ oper_status: 'down', monitored: false }), 'unmonitored_down')
assert.equal(m.classifyInterface(iface({ oper_status: 'down', monitored: false })).tone, 'neutral')
assert.equal(kind({ oper_status: 'notPresent' }), 'not_present')
assert.equal(kind({ oper_status: 'not_present' }), 'not_present')
assert.equal(kind({ oper_status: 'dormant' }), 'other')
assert.equal(m.classifyInterface(iface({ oper_status: 'dormant' })).label, 'Dormant')
assert.equal(kind({ oper_status: null }), 'other')
// Up wins over admin down (oper is checked first, like the server).
assert.equal(kind({ admin_status: 'down' }), 'up')
// Only the actionable set is coloured.
for (const over of [{ oper_status: 'down', admin_status: 'down' }, { oper_status: 'down', ever_up: false }, { oper_status: 'notPresent' }]) {
  assert.notEqual(m.classifyInterface(iface(over)).tone, 'crit')
}
assert.ok(m.STATUS_ORDER.down < m.STATUS_ORDER.up && m.STATUS_ORDER.up < m.STATUS_ORDER.unused)

/* ── Type groups ───────────────────────────────────────────────────────── */
const groups = { 6: 'ethernet', 62: 'ethernet', 117: 'ethernet', 135: 'vlan', 136: 'vlan', 131: 'tunnel', 161: 'lag', 71: 'wireless', 24: 'loopback', 53: 'virtual', 1: 'other', 23: 'other' }
for (const [t, g] of Object.entries(groups)) assert.equal(m.interfaceTypeGroup(Number(t)), g, `ifType ${t}`)
assert.equal(m.interfaceTypeGroup(null), 'other')
assert.equal(m.typeGroupLabel('lag'), 'Aggregate / LAG')

/* ── Names and descriptions ────────────────────────────────────────────── */
assert.equal(m.interfaceName(iface()), 'Gi0/1')
assert.equal(m.interfaceName(iface({ if_name: '', if_descr: 'port1' })), 'port1')
assert.equal(m.interfaceName(iface({ if_name: null, if_descr: null, if_index: 7 })), 'ifIndex 7')
assert.equal(m.interfaceDescription(iface({ if_alias: 'To core-sw1' })), 'To core-sw1')
assert.equal(m.interfaceDescription(iface({ if_alias: '' })), null)
assert.equal(m.interfaceDescription(iface({ if_alias: '   ' })), null)
assert.equal(m.interfaceDescription(iface({ if_alias: 'gi0/1' })), null, 'alias equal to ifName is no description')
assert.equal(m.interfaceDescription(iface({ if_alias: 'GigabitEthernet0/1' })), null, 'alias equal to ifDescr is no description')

/* ── Speed, utilisation and sparkline scale ────────────────────────────── */
assert.equal(m.effectiveSpeed(iface({ configured_speed_bps: 5e8 })), 5e8)
assert.equal(m.effectiveSpeed(iface({ if_speed: 0 })), null)
assert.equal(m.utilisationPct(4e8, 1e8, 1e9), 40, 'busier direction')
assert.equal(m.utilisationPct(null, 1e8, 1e9), null)
assert.equal(m.utilisationPct(1e8, 1e8, null), null)
assert.equal(m.utilisationPct(3e9, 0, 1e9), 100)
assert.deepEqual(m.sparkDomain([1, 5, null, 3], 1e9), [0, 1e9])
assert.deepEqual(m.sparkDomain([1, 5, null, 3], null), [0, 5])
assert.deepEqual(m.sparkDomain([2e9], 1e9), [0, 2e9], 'never clips a burst above the link speed')
assert.deepEqual(m.sparkDomain([null, 0], null), [0, 1])

/* ── Errors: unknown is never zero ─────────────────────────────────────── */
assert.equal(m.errorSum(null), null)
assert.equal(m.discardSum(undefined), null)
assert.equal(m.errorSum({ in_errors: 2, out_errors: 3, in_discards: 9, out_discards: 1 }), 5)
assert.equal(m.discardSum({ in_errors: 2, out_errors: 3, in_discards: 9, out_discards: 1 }), 10)

/* ── Saved filters ─────────────────────────────────────────────────────── */
assert.equal(m.parseSavedFilter('errors'), 'errors')
assert.equal(m.parseSavedFilter('bogus'), 'all')
assert.equal(m.parseSavedFilter(null), 'all')
assert.deepEqual(m.SAVED_FILTERS.map((f) => f.id), ['all', 'uplinks', 'errors', 'unexpected_down'])

for (const name of ['wan1', 'WAN', 'internet', 'isp2', 'port1-wan', 'Uplink to core']) assert.ok(m.looksLikeWan(iface({ if_name: name })), name)
assert.ok(m.looksLikeWan(iface({ if_alias: 'DIA circuit 1234' })))
for (const name of ['media0', 'display', 'swan', 'Gi0/1']) assert.ok(!m.looksLikeWan(iface({ if_name: name, if_descr: name })), name)

const rows = [
  iface({ if_index: 1, if_name: 'Gi1/0/1', if_speed: 1e9 }),
  iface({ if_index: 2, if_name: 'Gi1/0/2', if_speed: 1e9, oper_status: 'down' }),
  iface({ if_index: 3, if_name: 'Te1/1/1', if_speed: 1e10 }),
  iface({ if_index: 4, if_name: 'Te1/1/2', if_speed: 1e10, oper_status: 'down', ever_up: false }),
  iface({ if_index: 5, if_name: 'Vlan10', if_type: 136, if_speed: 1e11 }),
  iface({ if_index: 6, if_name: 'Gi1/0/3', if_alias: 'ISP-A', if_speed: 1e8 }),
]
assert.equal(m.uplinkSpeed(rows), 1e10, 'fastest up physical port; VLAN speeds ignored')
assert.equal(m.uplinkSpeed(rows.filter((r) => r.if_speed === 1e9)), null, 'one speed everywhere says nothing about uplinks')
const errorTotals = { 1: { in_errors: 0, out_errors: 0, in_discards: 0, out_discards: 0 }, 3: { in_errors: 0, out_errors: 0, in_discards: 4, out_discards: 0 }, 6: { in_errors: 1, out_errors: 0, in_discards: 0, out_discards: 0 } }
const ctx = { uplinkSpeed: m.uplinkSpeed(rows), errorTotals }
const pick = (f, c = ctx) => rows.filter((r) => m.matchesSavedFilter(r, f, c)).map((r) => r.if_index)
assert.deepEqual(pick('all'), [1, 2, 3, 4, 5, 6])
assert.deepEqual(pick('uplinks'), [3, 6], 'the up 10G port and the ISP-named port; the unused 10G port is not an uplink')
assert.deepEqual(pick('errors'), [3, 6], 'errors or discards > 0')
assert.deepEqual(pick('errors', { ...ctx, errorTotals: null }), [], 'unavailable counts never match')
assert.deepEqual(pick('unexpected_down'), [2])

assert.ok(m.matchesSearch(rows[5], 'isp'))
assert.ok(m.matchesSearch(rows[0], '  gi1/0/1 '))
assert.ok(m.matchesSearch(rows[2], '3'))
assert.ok(!m.matchesSearch(rows[0], 'te1'))
assert.ok(m.matchesSearch(rows[0], ''))

/* ── Sorting keeps unknown values last ─────────────────────────────────── */
const vals = [3, null, 1, 2]
assert.deepEqual([...vals].sort((a, b) => m.compareNullable(a, b, 1)), [1, 2, 3, null])
assert.deepEqual([...vals].sort((a, b) => m.compareNullable(a, b, -1)), [3, 2, 1, null])

/* ── The tab and the redirect (source checks) ──────────────────────────── */
const tab = fs.readFileSync('src/pages/device/InterfacesTab.tsx', 'utf8')
assert.match(tab, /export function InterfacesTab\(\{ device, range \}: \{ device: DeviceRecord; range: DeviceRange \}\)/)
assert.match(tab, /section="interfaces"/)
for (const id of ['interface-table', 'interface-inventory', 'interface-throughput', 'interface-talkers']) assert.ok(tab.includes(`id: '${id}'`), `widget id ${id} kept`)
assert.ok(tab.includes('/interfaces/error-totals?hours='))
assert.ok(tab.includes('TimeSeriesPanel') && tab.includes('formatBitRateAxis'), 'expansion chart uses the shared time-series panel')
assert.ok(!/TimeRangePicker|ArrowLeft/.test(tab), 'no page header, back link or range picker inside the shell')
assert.ok(!/text-\[(9|10|11)px\]/.test(tab), 'labels are at least 12px')
assert.ok(!/formatBps\b/.test(tab), 'shared formatters only')

const page = fs.readFileSync('src/pages/DeviceInterfacesPage.tsx', 'utf8')
assert.match(page, /export function DeviceInterfacesPage\(/)
assert.match(page, /<Navigate replace to=\{interfacesTabUrl\(/)
assert.equal(m.interfacesTabUrl('abc', ''), '/devices/abc?tab=interfaces')
assert.equal(m.interfacesTabUrl('abc', '?range=7d&tab=overview&from=2026-10-01T00%3A00%3A00Z'),
  '/devices/abc?range=7d&from=2026-10-01T00%3A00%3A00Z&tab=interfaces', 'keeps range/from, replaces a stale tab')
assert.equal(m.interfacesTabUrl('abc', new URLSearchParams('tab=a&tab=b&ifilter=errors')), '/devices/abc?ifilter=errors&tab=interfaces')

console.log('Device interfaces tests passed: status rules, type groups, descriptions, utilisation, saved filters, unknown errors, sorting, tab and redirect.')
