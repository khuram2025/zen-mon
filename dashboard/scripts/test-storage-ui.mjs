import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const source = fs.readFileSync('src/components/storage/storageMath.ts', 'utf8')
const compiled = await transformWithEsbuild(source, 'storageMath.ts', { loader: 'ts', format: 'esm' })
const m = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const GIB = 1024 ** 3
const EXTENT = 4 * 1024 ** 2

// Allocation: whole extents, never beyond free space, rejects nonsense.
assert.equal(m.allocationBytes('10', EXTENT, 100 * GIB), 10 * GIB)
assert.equal(m.allocationBytes('500', EXTENT, 20 * GIB + EXTENT / 2), 20 * GIB)
assert.equal(m.allocationBytes('1.0001', EXTENT, 100 * GIB) % EXTENT, 0)
for (const bad of ['', ' ', '0', '-3', 'abc']) assert.equal(m.allocationBytes(bad, EXTENT, GIB), null)

// Space breakdown: a shared OS+data volume attributes the remainder to "other".
const shared = { mount: '/', label: 'System & data volume', roles: ['system', 'data'],
  total_bytes: 96 * GIB, used_bytes: 72 * GIB, free_bytes: 24 * GIB, usage_percent: 75 }
const consumers = [
  { id: 'clickhouse', label: 'Metrics', bytes: 16 * 1024 ** 2, mount: '/data', hint: null },
  { id: 'journal', label: 'Journal', bytes: 2 * GIB, mount: '/', hint: null },
  { id: 'backups', label: 'Backups', bytes: 0, mount: '/data', hint: null },
]
const segments = m.spaceSegments(shared, consumers, true)
assert.equal(segments[0].id, 'other')
assert.equal(segments.reduce((a, s) => a + s.bytes, 0), 72 * GIB)
assert(!segments.some((s) => s.id === 'backups'), 'empty consumers are hidden')
// Separate volumes: each only shows its own consumers.
const data = { ...shared, mount: '/data', roles: ['data'], label: 'Data volume' }
assert.deepEqual(m.spaceSegments(data, consumers, false).map((s) => s.id), ['other', 'clickhouse'])

// Forecast wording.
assert.equal(m.forecastSummary(null), null)
assert.equal(m.forecastSummary({ daily_bytes: GIB, current_bytes: 10 * GIB, steady_state_bytes: 30 * GIB,
  growth_remaining_bytes: 20 * GIB, free_bytes: 50 * GIB, fits: true, days_until_full: null,
  unbounded_categories: [] }).tone, 'ok')
const tight = m.forecastSummary({ daily_bytes: GIB, current_bytes: 10 * GIB, steady_state_bytes: 30 * GIB,
  growth_remaining_bytes: 20 * GIB, free_bytes: 5 * GIB, fits: false, days_until_full: 5, unbounded_categories: [] })
assert.equal(tight.tone, 'critical')
assert.match(tight.text, /about 5 days/)
assert.match(tight.text, /Historical metrics footprint/)
assert.match(tight.text, /estimated free space/)
assert(!tight.text.includes('Metrics grow'))

// Activity: repeated no-op auto-purges collapse into one row.
const noop = (id, at) => ({ id, created_at: at, event_type: 'auto_purge', actor: 'system', freed_bytes: 0,
  freed_human: '0 B', details: { usage_percent: 86, note: 'nothing purgeable' } })
const grouped = m.groupEvents([noop(3, '2026-10-08T09:10:00Z'), noop(2, '2026-10-08T09:05:00Z'),
  noop(1, '2026-10-08T09:00:00Z'),
  { id: 0, created_at: '2026-10-08T08:00:00Z', event_type: 'disk_added', actor: 'admin', freed_bytes: 0,
    freed_human: '0 B', details: { disk: '/dev/sdb', pv_size: 50 * GIB, added_bytes: 50 * GIB } }])
assert.equal(grouped.length, 2)
assert.equal(grouped[0].count, 3)
assert.equal(grouped[0].oldest, '2026-10-08T09:00:00Z')
assert.equal(m.eventSummary(grouped[1].event), '50.0 GiB added · 50.0 GiB allocated')
// Watchdog passes report scheduled bytes, measured reclamation separately.
const purge = { id: 9, created_at: '2026-10-10T10:00:00Z', event_type: 'watchdog_purge', actor: 'watchdog',
  freed_bytes: 0, freed_human: '0 B', details: { scheduled_bytes: 3 * GIB, reason: ['usage 90.0% ≥ 85%'],
    actions: [{ table: 'system.text_log' }, { table: 'system.text_log' }, { table: 'zenplus.flow_records' }] } }
assert.equal(m.eventSummary(purge), '3.0 GiB scheduled for deletion · Low-space protection')
const reclaim = { ...purge, event_type: 'watchdog_reclaim', details: { scheduled_bytes: 3 * GIB, measured_free_change_bytes: -GIB } }
assert.match(m.eventSummary(reclaim), /free space −1\.0 GiB measured/)
for (const type of ['disk_added', 'volume_extended', 'space_claimed', 'disk_removed', 'auto_purge_skipped',
  'watchdog_purge', 'watchdog_reclaim', 'watchdog_exhausted', 'watchdog_ingest_paused']) {
  assert(m.EVENT_LABELS[type], `label for ${type}`)
}

console.log('Storage UI regressions passed: allocation rounding, space breakdown, forecast, activity grouping.')

// Unknown/raw backend text must never become customer-visible service detail.
const secretDetail = 'ClickHouse PostgreSQL /dev/sda /data/clickhouse system.text_log LVM Traceback'
for (const event_type of [...Object.keys(m.EVENT_LABELS), secretDetail]) {
  const text = m.eventSummary({ event_type, freed_bytes: 0, details: {
    error: secretDetail, message: secretDetail, note: secretDetail, reason: secretDetail,
    disk: secretDetail, pv: secretDetail, mount: secretDetail, extend_error: secretDetail,
    actions: [{ table: secretDetail }],
  } })
  assert(!/ClickHouse|PostgreSQL|\/dev\/|\/data\/|text_log|LVM|Traceback/.test(text), text)
}
for (const status of [400, 401, 403, 409, 422, 500, 503, undefined]) {
  assert(!m.storageError({ response: { status, data: { detail: secretDetail } } }).includes(secretDetail))
}
assert.equal(m.consumerLabel('clickhouse'), 'Monitoring history')
assert.equal(m.consumerLabel('clickhouse_diagnostic'), 'Appliance logs')
assert.equal(m.consumerLabel(secretDetail), 'Other storage')
for (const code of ['clickhouse_drift', 'watchdog_error', 'pv_missing', secretDetail]) {
  assert(!m.issueText({ code, title: secretDetail, detail: secretDetail }).join(' ').includes(secretDetail))
}
const logs = [
  { table: 'text_log_0', bytes: GIB, legacy: true, retention_days: 3, expires_after: '2026-10-14' },
  { table: 'trace_log_0', bytes: 2 * GIB, legacy: true, retention_days: 3, expires_after: '2026-10-14' },
  { table: 'query_log_pre_recovery_20261007', bytes: GIB, evidence: true },
  { table: 'error_log_0', bytes: 100, legacy: true, protected: true, retention_days: 30 },
]
const groups = m.logGroups(logs)
assert.equal(groups.length, 3)
assert.equal(groups[0].bytes, 3 * GIB)
assert.match(groups[0].policy, /2026/)
assert.match(groups[1].policy, /support review/)
assert.match(groups[2].policy, /protected/)
assert.equal(groups.reduce((sum, g) => sum + g.bytes, 0), 4 * GIB + 100)
assert(!JSON.stringify(groups).includes('text_log'))
assert.equal(m.diskName({ disks: [{ path: '/dev/sda', pvs: [{ path: '/dev/sda3' }] }] }, '/dev/sda3'), 'Disk 1')
console.log('Customer storage presentation: raw errors, unknown types, labels, expiry grouping and disk identity passed.')

await import('./test-storage-render.mjs')
