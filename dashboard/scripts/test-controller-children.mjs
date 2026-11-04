// Controller-managed children (FortiGate-managed FortiSwitches and FortiAPs): the device page tab, the wording of
// controller-sourced readings, availability with coverage, and the list's separate Availability / Uptime columns.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

async function load(path) {
  const src = fs.readFileSync(path, 'utf8')
  const compiled = await transformWithEsbuild(src, path.split('/').pop(), { loader: 'ts', format: 'esm' })
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
}

const tabs = await load('src/pages/device/tabs.ts')
const m = await load('src/pages/device/controllerModel.ts')
const ids = (list) => list.map((t) => t.id)

/* ── Tab ─────────────────────────────────────────────────────────────────── */
const ap = { id: 'ap', hostname: 'AP01', snmp_enabled: false, ping_enabled: false, device_type: 'access_point',
  poll_mode: 'via_controller', managed_by_device_id: 'fgt' }
assert.deepEqual(ids(tabs.deviceTabs(ap)), ['summary', 'performance', 'managed', 'events', 'configuration'],
  'a managed child gets its controller tab; no Interfaces/Hardware, it is not polled itself')
assert.equal(tabs.deviceTabs(ap).find((t) => t.id === 'managed').label, 'Radios & clients')
assert.equal(tabs.deviceTabs({ ...ap, device_type: 'switch' }).find((t) => t.id === 'managed').label, 'Ports & PoE')
assert.ok(!ids(tabs.deviceTabs({ ...ap, poll_mode: 'direct' })).includes('managed'), 'a directly polled device keeps its tabs')
assert.equal(tabs.resolveTab('managed', tabs.deviceTabs(ap)), 'managed')

/* ── Source and staleness wording ────────────────────────────────────────── */
const ctrl = { id: 'fgt', hostname: 'SNIH-FGT201F', status: 'up', model: 'FortiGate-201F' }
assert.equal(m.sourceText({ controller: ctrl, reported: true, age_s: 40, stale: false, stale_after_s: 600 }),
  'Reported by SNIH-FGT201F 40 s ago')
assert.equal(m.sourceText({ controller: ctrl, reported: true, age_s: 1500, stale: true, stale_after_s: 600 }),
  'Reported by SNIH-FGT201F 25 min ago · stale (older than 10 min)')
assert.equal(m.sourceText({ controller: ctrl, reported: false }), 'Not reported by SNIH-FGT201F yet')

/* ── Availability: a percentage only with enough controller-observed history ─ */
assert.deepEqual(m.availabilityText({ pct: 99.65, coverage_pct: 100, sufficient: true, samples: 288, basis: 'controller' }),
  { value: '99.65%', sub: 'Controller-reported state · 100% of 24 h observed' })
assert.deepEqual(m.availabilityText({ pct: null, coverage_pct: 48.6, sufficient: false, samples: 140, basis: 'controller' }),
  { value: 'Insufficient history', sub: '49% of the last 24 h observed by the controller' })
assert.equal(m.availabilityText(null).value, '—')

/* ── Panels ───────────────────────────────────────────────────────────────── */
assert.ok(m.isSwitchPanel({ supported: true, ports: [] }) && !m.isApPanel({ supported: true, ports: [] }))
assert.ok(m.isApPanel({ supported: true, radios: [] }))
assert.ok(!m.isSwitchPanel({ supported: false, reason: 'x' }) && !m.isSwitchPanel(null))
assert.equal(m.formatWatts(6.2), '6.2 W')
assert.equal(m.formatWatts(null), '—', 'no PoE reading is unknown, not 0 W')
assert.equal(m.portList(['port1', 'port2', 'port3', 'port4', 'port5']), 'port1, port2, port3 +2')
assert.equal(m.signalTone('poor'), 'crit')
assert.equal(m.signalTone(null), 'none')
const history = { hours: 24, series: [{ key: 'cpu', points: [{ ts: '2026-11-04T10:00:00Z', value: 22 }] }] }
assert.deepEqual(m.historyPoints(history, 'cpu'), [{ ts: Date.parse('2026-11-04T10:00:00Z'), value: 22 }])
assert.deepEqual(m.historyPoints(history, 'memory'), [])

/* ── Devices list: two measurements, two labels ─────────────────────────── */
const page = fs.readFileSync('src/pages/DevicesPage.tsx', 'utf8')
assert.match(page, /Availability <span className="font-normal text-muted">\(\{rangeLabel\}\)<\/span>/, 'the percentage column says Availability')
assert.match(page, /Uptime <span className="font-normal text-muted">\(since boot\)<\/span>/, 'the duration column says since boot')
assert.ok(!/fallback=\{uptime\}/.test(page), 'the availability cell never falls back to a duration')
assert.match(page, /Insufficient history/, 'a child without enough controller history says so')
assert.match(page, /via controller/, 'controller-sourced CPU/memory is marked')

console.log('Controller children passed: managed tab, source and staleness wording, availability coverage, panels and list columns.')
