// Settings -> System status: the health helpers and the tab's address rules.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

async function load(entry) {
  const compiled = await build({ entryPoints: [entry], bundle: true, write: false, format: 'esm', platform: 'browser',
    alias: { '@': path.resolve('src') }, define: { 'process.env.NODE_ENV': '"production"' } })
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
}
const m = await load('src/components/system/healthMath.ts')
const s = await load('src/components/system/sections.ts')
const GIB = 1024 ** 3

// A reading that could not be taken is a dash, never zero.
for (const missing of [null, undefined, NaN]) {
  assert.equal(m.pct(missing), '—')
  assert.equal(m.fmtBytes(missing), '—')
  assert.equal(m.duration(missing), '—')
  assert.equal(m.cpuTime(missing), '—')
  assert.equal(m.usageTone(missing, 75, 90), 'muted')
}
assert.equal(m.pct(0), '0.0%')          // a real zero stays a number
assert.equal(m.pct(4.26), '4.3%')
assert.equal(m.pct(93.1), '93%')
assert.equal(m.fmtBytes(0), '0 B')
assert.equal(m.fmtBytes(1.5 * GIB), '1.5 GiB')
assert.equal(m.fmtBytes(-1), '—')
assert.equal(m.duration(93784), '1d 2h')
assert.equal(m.duration(3 * 3600 + 12 * 60), '3h 12m')
assert.equal(m.duration(45), '45s')
assert.equal(m.cpuTime(7505), '2h 05m')
assert.equal(m.cpuTime(192), '3m 12s')

// Tiles are quiet until a reading needs attention.
assert.equal(m.usageTone(40, 75, 90), 'primary')
assert.equal(m.usageTone(80, 75, 90), 'warning')
assert.equal(m.usageTone(95, 75, 90), 'danger')
// Load is judged per core.
assert.equal(m.loadTone([4, 4, 4], 16), 'primary')
assert.equal(m.loadTone([4, 4, 4], 2), 'warning')
assert.equal(m.loadTone([9, 9, 9], 2), 'danger')
assert.equal(m.loadTone(null, 2), 'muted')
assert.equal(m.loadTone([1, 1, 1], null), 'muted')

// Services.
const svc = (state, over = {}) => ({ id: 'x', label: 'X', core: true, state, detail: null, uptime_s: 1, memory_bytes: 1, cpu_seconds: 1, restarts: 0, ...over })
assert.equal(m.serviceStateLabel(svc('down', { detail: 'not answering' })), 'Down (not answering)')
assert.equal(m.serviceStateLabel(svc('running', { detail: 'ignored' })), 'Running')
assert.equal(m.serviceStateLabel(svc('unknown')), 'Could not be checked')
assert.equal(m.serviceStateLabel(svc('brand-new-state')), 'Could not be checked')
assert.deepEqual(m.serviceSummary([svc('running'), svc('unknown'), svc('down'), svc('stopped', { core: false }), svc('down', { core: false })]),
  { running: 1, total: 3, down: 2, unknown: 1 })

// History: gaps stay gaps.
const points = [
  { t: 100, cpu: 10, cpu_max: 60, iowait: 1, mem: 50, mem_max: 55, swap: 0, load: 1, disk: 60 },
  { t: 400, cpu: null, cpu_max: null, iowait: null, mem: null, mem_max: null, swap: null, load: null, disk: null },
  { t: 700, cpu: 30, cpu_max: 40, iowait: 1, mem: 70, mem_max: 71, swap: 0, load: 1, disk: 60 },
]
assert.deepEqual(m.chartSeries(points, 'cpu'), [[100000, 10], [400000, null], [700000, 30]])
assert.deepEqual(m.seriesStats(points, 'cpu', 'cpu_max'), { mean: 20, peak: 60 })
assert.equal(m.seriesStats([points[1]], 'cpu', 'cpu_max'), null)
assert.deepEqual(m.historyCoverage({ points, bucket_s: 300, hours: 24 }), { covered_s: 600, complete: false })
assert.equal(m.historyCoverage({ points: Array.from({ length: 288 }, () => points[0]), bucket_s: 300, hours: 24 }).complete, true)

// Storage change over the window, in words.
assert.equal(m.storageChange(null), null)
assert.equal(m.storageChange({ bytes: 2.4 * GIB, hours: 23.5 }), 'Storage use grew by 2.4 GiB over the last 24 hours.')
assert.equal(m.storageChange({ bytes: -GIB, hours: 6 }), 'Storage use fell by 1.0 GiB over the last 6 hours.')
assert.equal(m.storageChange({ bytes: 1000, hours: 1 }), 'Storage use is unchanged over the last 1 hour.')

assert.equal(m.updateStatusLabel('success'), 'installed')
assert.equal(m.updateStatusLabel('rolled_back'), 'rolled back')
assert.equal(m.clockLabel({ now: '', timezone: null, ntp_synchronized: null }).tone, 'muted')
assert.equal(m.clockLabel({ now: '', timezone: null, ntp_synchronized: false }).tone, 'warning')
assert.equal(m.clockLabel({ now: '', timezone: null, ntp_synchronized: true }).tone, 'primary')
console.log('System status helpers: missing readings, tones, services, history gaps and wording passed.')

// The tab keeps its address; old links open what they used to.
assert.equal(s.SYSTEM_STATUS_TAB, 'storage')
assert.equal(s.sectionFromParam(null), 'health')                 // ?tab=storage alone: the overview of the page
assert.equal(s.sectionFromParam('disks'), 'disks')               // old deep links are unchanged
for (const old of ['disks', 'retention', 'backups', 'activity']) assert.equal(s.sectionFromParam(old), old)
assert.equal(s.sectionFromParam('overview'), 'storage')
assert.equal(s.sectionFromParam('nonsense'), 'health')
assert.equal(s.sectionParam('health'), null)
assert.equal(s.sectionParam('storage'), 'storage')
assert.equal(s.systemStatusHref(), '/settings/general?tab=storage')
assert.equal(s.systemStatusHref('retention'), '/settings/general?tab=storage&section=retention')
for (const alias of ['system', 'system-status', 'status', 'health']) assert.equal(s.canonicalTab(alias), 'storage')
assert.equal(s.canonicalTab('storage'), 'storage')
assert.equal(s.canonicalTab('licenses'), 'licenses')
assert.equal(s.canonicalTab(null), null)
assert.deepEqual(s.SYSTEM_SECTIONS.map((x) => x.label), ['Overview', 'Storage', 'Disks & capacity', 'Retention & cleanup', 'Backups', 'Activity'])

// The rename reached every place the name is shown, and the address and permission did not change.
const read = (p) => fs.readFileSync(p, 'utf8')
const nav = read('src/components/layout/navigation.ts')
assert.match(nav, /settingsTab\('storage', 'System status', \w+, 'system\.admin'\)/)
assert(!/settingsTab\('storage', 'Storage'/.test(nav))
const page = read('src/pages/GeneralSettingsPage.tsx')
assert.match(page, /\{ value: 'storage', label: 'System status', icon: \w+, permission: 'system\.admin' \}/)
assert.match(page, /SYSTEM_STATUS_TAB_ALIASES\.includes/)
const app = read('src/App.tsx')
assert.match(app, /GENERAL_TABS = new Set\(\[[^\]]*'storage'/)            // /settings/storage still redirects to the tab
assert.match(app, /'system-status': '\/settings\/general\?tab=storage'/)
const catalog = JSON.parse(read('../server/app/core/page_catalog.json'))
const entry = catalog.pages.find((p) => p.key === '/settings/general?tab=storage')
assert(entry, 'the page catalogue keeps the tab under its address')
assert.equal(entry.label, 'System status')
assert.deepEqual(entry.gates.at(-1).any, ['system.admin'], 'the page still needs system.admin')
// The overview names services by what they do. The software behind them is not written into the page's source.
for (const file of ['src/components/system/HealthSection.tsx', 'src/components/system/healthMath.ts', 'src/components/system/HealthHistoryChart.tsx', 'src/components/SystemStatusTabContent.tsx']) {
  const visible = read(file).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert(!/ClickHouse|PostgreSQL|Postgres|Redis|nginx/i.test(visible), `${file} names the software behind a service`)
}
console.log('System status tab: renamed everywhere, address and permission unchanged, old links resolve.')
