import assert from 'node:assert/strict'
import fs from 'node:fs'
import { build } from 'esbuild'
import path from 'node:path'

const compiled = await build({ entryPoints: ['src/components/storage/storageMath.ts'],
  bundle: true, write: false, format: 'esm', platform: 'browser',
  alias: { '@': path.resolve('src') }, define: { 'process.env.NODE_ENV': '"production"' } })
const m = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
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

// A missing reading is unavailable, never zero; a real zero stays "0 B".
for (const missing of [null, undefined, NaN, Infinity]) assert.equal(m.bytes(missing), '—')
assert.equal(m.bytes(0), '0 B')
assert.equal(m.bytes(-5), '0 B')
assert.equal(m.bytes(GIB), '1.0 GiB')
assert.equal(m.bytes(512), '512 B')

// Claim + allocate: only what the claim added, whole extents, nothing when nothing was added.
assert.equal(m.claimedAllocation(10 * GIB + EXTENT / 2, EXTENT), 10 * GIB)
assert.equal(m.claimedAllocation(10 * GIB, 0), 10 * GIB)
for (const none of [0, EXTENT - 1, -GIB, null, undefined, '5', NaN]) assert.equal(m.claimedAllocation(none, EXTENT), null)
// Add disk: a custom amount at or within one extent of the maximum asks for all.
assert.equal(m.addDiskAllocation('all', null, EXTENT, 50 * GIB), 'all')
assert.equal(m.addDiskAllocation('none', 10 * GIB, EXTENT, 50 * GIB), 'none')
assert.equal(m.addDiskAllocation('custom', 10 * GIB, EXTENT, 50 * GIB), String(10 * GIB))
assert.equal(m.addDiskAllocation('custom', 50 * GIB, EXTENT, 50 * GIB), 'all')
assert.equal(m.addDiskAllocation('custom', 50 * GIB - EXTENT, EXTENT, 50 * GIB), 'all')
assert.equal(m.addDiskAllocation('custom', 50 * GIB - 2 * EXTENT, EXTENT, 50 * GIB), String(50 * GIB - 2 * EXTENT))
assert.equal(m.addDiskAllocation('custom', 50 * GIB, EXTENT, 50 * GIB + EXTENT / 2), 'all')
assert.equal(m.addDiskAllocation('custom', null, EXTENT, 50 * GIB), 'none')

// Forecast horizons read as sentences.
const horizon = (days, fits = false, unbounded = []) => m.forecastSummary({ daily_bytes: GIB, current_bytes: 10 * GIB,
  steady_state_bytes: 30 * GIB, growth_remaining_bytes: 20 * GIB, free_bytes: 5 * GIB, fits, days_until_full: days,
  unbounded_categories: unbounded }).text
assert.match(horizon(1), /runs out in about 1 day\.$/)
assert.match(horizon(0), /runs out in less than a day\.$/)
assert.match(horizon(51200), /runs out in more than a year\.$/)
assert.match(horizon(365), /about 365 days/)
assert.match(horizon(1, false, ['flows']), /lasts about 1 day\.$/)
assert.match(horizon(0, false, ['flows']), /lasts less than a day\.$/)
assert(!/0 days|1 days|51200/.test(horizon(0) + horizon(1) + horizon(51200)))

// Activity: one amount per row, honest titles.
const ev = (event_type, details = {}, freed_bytes = 0) => ({ id: 1, created_at: '2026-11-19T10:00:00Z', event_type,
  actor: 'admin', freed_bytes, freed_human: '', details })
const sweep = ev('watchdog_purge', { scheduled_bytes: GIB, reason: 'legacy_cleanup' })
assert.equal(m.eventLabel(sweep), 'Log cleanup')
assert.equal(m.lowSpacePurge(sweep), false)
assert.equal(m.eventSummary(sweep), '1.0 GiB scheduled for deletion · Expired appliance logs removed on schedule')
assert.equal(m.eventLabel(purge), 'Low-space purge')
assert.equal(m.lowSpacePurge(purge), true)
assert.equal(m.eventLabel(ev('watchdog_purge', { reason: [] })), 'Log cleanup')
assert.equal(m.eventLabel(ev('watchdog_purge', {})), 'Log cleanup')
assert.equal(m.eventLabel(ev('backup_created')), 'Backup created')
assert.equal(m.eventLabel(ev(secretDetail)), 'Storage operation')
assert.equal(m.eventSummary(ev('manual_purge', { scheduled_bytes: 2 * GIB }, 2 * GIB)), '2.0 GiB scheduled for deletion')
assert.equal(m.eventSummary(ev('auto_purge', {}, GIB)), '1.0 GiB reclaimed')
assert.equal(m.eventSummary(ev('something_new', {}, GIB)), '1.0 GiB reclaimed')
assert.equal(m.eventSummary(ev('something_new')), 'Storage operation recorded.')
assert.equal(m.eventSummary(ev('retention_applied', { changes: [{ table: 'flow_records', from: 30, to: 14 }] })),
  'Retention settings applied to monitoring history.')
const partial = m.eventSummary(ev('retention_applied', { changes: [{ table: 'a', from: 30, to: 14 },
  { table: 'flow_records', error: secretDetail }, { table: 'snmp_metrics', error: secretDetail }] }))
assert.equal(partial, 'Retention applied; 2 tables could not be updated')
assert.equal(m.eventSummary(ev('retention_applied', { changes: [{ table: 'flow_records', error: 'x' }] })),
  'Retention not applied; 1 table could not be updated')

// Backup routes: a short plain server sentence reaches the operator; anything else stays generic.
const backupErr = (status, detail, url = '/system/storage/backups') => m.storageError({ response: { status, data: { detail } }, config: { url } })
assert.equal(backupErr(409, 'A backup is already running'), 'A backup is already running.')
assert.equal(backupErr(410, 'Backup files are missing on disk', '/system/storage/backups/abc/restore'), 'Backup files are missing on disk.')
assert.equal(backupErr(400, 'Choose at least one item to restore.', '/system/storage/backups/abc/restore'), 'Choose at least one item to restore.')
const generic409 = m.storageError({ response: { status: 409 } })
const generic400 = m.storageError({ response: { status: 400 } })
assert.equal(backupErr(409, 'A backup is already running', '/system/storage/extend'), generic409)
assert.equal(backupErr(409, secretDetail), generic409)
assert.equal(backupErr(400, 'This backup does not include ClickHouse data'), generic400)
assert.equal(backupErr(400, 'Backup cannot be restored: backup has no clickhouse_schema.sql'), generic400)
assert.equal(backupErr(400, "Backup cannot be restored: [Errno 13] Permission denied: '/data/backups/x'"), generic400)
assert.equal(backupErr(400, 'x'.repeat(200)), generic400)
assert.equal(backupErr(400, { msg: 'A backup is already running' }), generic400)
assert.equal(backupErr(400, [{ loc: ['body'], msg: 'field required' }]), generic400)
assert(!backupErr(500, 'A backup is already running').includes('already running'))
assert(!backupErr(404, 'Backup not found').includes('Backup not found'))

// Separate volumes: each low-space finding names its capacity; the server title itself is never shown.
const issue = (code, title) => m.issueText({ code, title, detail: secretDetail, severity: 'warning', action: 'expand' })
assert.equal(issue('usage_high', 'Data volume is 90% full')[0], 'Monitoring capacity is running low')
assert.equal(issue('usage_high', 'System volume is 88% full')[0], 'System capacity is running low')
assert.equal(issue('usage_critical', 'Data volume is 97% full')[0], 'Monitoring capacity is critically low')
assert.equal(issue('usage_elevated', 'System volume is 80% full')[0], 'Plan for more system capacity')
assert.equal(issue('usage_high', 'Storage is 90% full')[0], 'Storage is running low')
assert.equal(issue('pvmove', 'Data volume is moving')[0], 'Data is moving between disks')
assert(!issue('usage_high', 'Data volume is 90% full').join(' ').includes('90%'))

// Rescan: the toast says what was found.
assert.equal(m.rescanSummary({ available_disks: [{}, {}], layout: { claimable: [{}] } }),
  '2 new disks detected · 1 enlarged disk with space to claim.')
assert.equal(m.rescanSummary({ available_disks: [{}], layout: { claimable: [] } }), '1 new disk detected.')
assert.equal(m.rescanSummary({ available_disks: [], layout: { claimable: [] } }), 'No new disks or size changes detected.')
assert.equal(m.rescanSummary({ status: 'ok', message: secretDetail }), null)
assert.equal(m.rescanSummary(undefined), null)

// Low-space protection: the server's ranges, whole numbers except the GiB reserve.
const policy = { enabled: true, threshold_pct: 85, target_pct: 75, min_keep_days: 7, min_free_gib: 5 }
assert.equal(m.autoPurgeError(policy), null)
assert.equal(m.autoPurgeError({ ...policy, min_free_gib: 0.5 }), null)
assert.equal(m.autoPurgeError({ ...policy, min_free_gib: 2.25 }), null)
assert.equal(m.autoPurgeError({ ...policy, threshold_pct: 85.5 }).field, 'threshold_pct')
assert.equal(m.autoPurgeError({ ...policy, threshold_pct: 99 }).field, 'threshold_pct')
assert.equal(m.autoPurgeError({ ...policy, threshold_pct: NaN }).field, 'threshold_pct')
assert.equal(m.autoPurgeError({ ...policy, target_pct: 70.1 }).field, 'target_pct')
assert.equal(m.autoPurgeError({ ...policy, target_pct: 39 }).field, 'target_pct')
assert.match(m.autoPurgeError({ ...policy, target_pct: 85 }).message, /below the threshold/)
assert.equal(m.autoPurgeError({ ...policy, min_keep_days: 1.5 }).field, 'min_keep_days')
assert.equal(m.autoPurgeError({ ...policy, min_keep_days: 366 }).field, 'min_keep_days')
assert.equal(m.autoPurgeError({ ...policy, min_free_gib: 0.4 }).field, 'min_free_gib')
assert.equal(m.autoPurgeError({ ...policy, min_free_gib: NaN }).field, 'min_free_gib')
assert.equal(m.autoPurgeError({ ...policy, enabled: false, threshold_pct: NaN }), null)
// Switched off: the saved numbers go back, never the blank or half-typed ones.
assert.deepEqual(m.autoPurgePayload({ ...policy, enabled: false, threshold_pct: NaN, min_keep_days: 2.5 }, policy),
  { ...policy, enabled: false })
assert.deepEqual(m.autoPurgePayload({ ...policy, threshold_pct: 90 }, policy), { ...policy, threshold_pct: 90 })
assert(!JSON.stringify(m.autoPurgePayload({ ...policy, enabled: false, target_pct: NaN }, policy)).includes('null'))

// Schedule hint is dated today, so the local time uses the offset in force now.
const at = m.scheduleInstant(2, new Date('2026-07-15T23:30:00Z'))
assert.equal(at.toISOString(), '2026-07-15T02:00:00.000Z')
assert.equal(m.scheduleInstant(23).getUTCFullYear(), new Date().getUTCFullYear())

// Purge result: failed removals are counted, never reported as plain success.
assert.deepEqual(m.purgeOutcome({ scheduled_bytes: 2 * GIB, errors: [] }),
  { kind: 'success', title: 'Purge scheduled', detail: '2.0 GiB scheduled; capacity may take about 10 minutes to return' })
assert.equal(m.purgeOutcome({ freed_bytes: GIB }).kind, 'success')
const nothing = m.purgeOutcome({ scheduled_bytes: 0, freed_bytes: 0, errors: [secretDetail, secretDetail] })
assert.equal(nothing.kind, 'error')
assert.match(nothing.detail, /^2 removals failed; nothing was scheduled\./)
const some = m.purgeOutcome({ scheduled_bytes: GIB, errors: [secretDetail] })
assert.equal(some.kind, 'warning')
assert.match(some.detail, /^1 removal failed; 1\.0 GiB scheduled/)
assert(!JSON.stringify([nothing, some]).includes('ClickHouse'))

// Protection card: a 15-minute sample is named as such; hours only when close.
assert.equal(m.growthNote(null, null), 'Collecting a growth sample.')
assert.equal(m.growthNote(0, null), 'No net growth over the last 15 minutes.')
assert.equal(m.growthNote(2 * GIB, 174.3), 'Growth over the last 15 minutes: 2.0 GiB/hour; short samples swing with background maintenance.')
assert.equal(m.growthNote(2 * GIB, null), 'Growth over the last 15 minutes: 2.0 GiB/hour; short samples swing with background maintenance.')
assert.equal(m.growthNote(2 * GIB, 71.96), 'Growth over the last 15 minutes: 2.0 GiB/hour. Estimated remaining capacity: 72.0 hours at this rate.')
assert.match(m.growthNote(2 * GIB, 5.5), /Estimated remaining capacity: 5\.5 hours at this rate\.$/)
assert(!/remaining/.test(m.growthNote(2 * GIB, 72)))

// Log lifecycle: the two current retention classes carry different names.
const current = m.logGroups([
  { table: 'query_log', bytes: GIB, retention_days: 7, ttl_active: true },
  { table: 'metric_log', bytes: GIB, retention_days: 7, ttl_active: true },
  { table: 'text_log', bytes: 2 * GIB, retention_days: 3, ttl_active: true },
  { table: 'error_log', bytes: 100, retention_days: 30, ttl_active: true, protected: true },
])
assert.deepEqual(current.map((g) => [g.label, g.bytes, g.policy]), [
  ['Activity and query logs', 2 * GIB, 'Rolling 7-day retention'],
  ['Detailed trace logs', 2 * GIB, 'Rolling 3-day retention'],
  ['Incident logs', 100, 'Rolling 30-day retention'],
])
assert.equal(new Set(current.map((g) => g.label)).size, current.length)
assert(!/_log/.test(JSON.stringify(current)))
console.log('Storage review fixes: missing readings, claim/add allocation, wording, backup errors, validation and labels passed.')

await import('./test-storage-render.mjs')
