// Maintenance windows UI model (docs/maintenance-windows.md, src/components/maintenance/model.ts):
//   recurring and one-off request bodies for a device or a server, validation against the server's limits
//   (ISO weekdays, 1 minute to 7 days, HH:MM, until not in the past), the schedule sentence the server also
//   returns, the current/next occurrence shown for a listed window, and which windows this page may remove.
// Run from dashboard/: node scripts/test-maintenance-windows.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/components/maintenance/model.ts', 'utf8')
assert(!/^import (?!type )/m.test(src), 'model.ts imports types only, so it runs without the app')
const compiled = await transformWithEsbuild(src, 'model.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

const device = { kind: 'device', id: 'd-1', name: 'core-sw-01' }
const server = { kind: 'server', id: 's-1', name: 'backup-01' }
const today = '2026-11-19'

// Defaults: every night at 02:00 for an hour in the organisation's zone.
{
  const d = M.defaultRecurrence('Asia/Riyadh')
  assert.deepEqual(d.days, [1, 2, 3, 4, 5, 6, 7])
  assert.equal(d.startTime, '02:00')
  assert.equal(d.durationMinutes, 60)
  assert.equal(M.recurrenceError(d, today), null)
  assert.equal(M.describeRecurrence(d, 'Asia/Riyadh'), 'Every day at 02:00 for 1 h (Asia/Riyadh)')
}

// Day toggles stay sorted and unique; presets are recognised.
{
  assert.deepEqual(M.toggleDay([3, 1], 2), [1, 2, 3])
  assert.deepEqual(M.toggleDay([1, 2, 3], 2), [1, 3])
  assert(M.sameDays([1, 2, 3, 4, 5], M.DAY_PRESETS.find((p) => p.key === 'weekdays').days))
  assert.equal(M.WEEKDAYS.length, 7)
  assert.equal(M.WEEKDAYS[6].short, 'Sun')
}

// Wording matches the server's schedule sentence (maintenance_windows.describe).
{
  assert.equal(M.durationLabel(45), '45 min')
  assert.equal(M.durationLabel(120), '2 h')
  assert.equal(M.durationLabel(90), '1 h 30 min')
  const d = { days: [3, 1], startTime: '22:30', durationMinutes: 90, timezone: '', until: '2026-12-31' }
  assert.equal(M.describeRecurrence(d, 'UTC'), 'Every Mon, Wed at 22:30 for 1 h 30 min (UTC) until 2026-12-31')
}

// Validation mirrors the server's limits.
{
  const ok = M.defaultRecurrence('UTC')
  const err = (patch) => M.recurrenceError({ ...ok, ...patch }, today)
  assert.match(err({ days: [] }), /at least one day/)
  assert.match(err({ days: [0] }), /Monday to Sunday/)
  assert.match(err({ days: [8] }), /Monday to Sunday/)
  assert.match(err({ startTime: '24:00' }), /HH:MM/)
  assert.match(err({ startTime: '2:00' }), /HH:MM/)
  assert.match(err({ durationMinutes: 0 }), /at least 1 minute/)
  assert.match(err({ durationMinutes: 1.5 }), /at least 1 minute/)
  assert.match(err({ durationMinutes: M.MAX_DURATION_MINUTES + 1 }), /at most 7 days/)
  assert.equal(err({ durationMinutes: M.MAX_DURATION_MINUTES }), null)
  assert.match(err({ until: '2026-11-18' }), /in the past/)
  assert.equal(err({ until: today }), null)
  assert.match(err({ until: '19/11/2026' }), /YYYY-MM-DD/)
}

// Request bodies: scope by asset kind; a blank zone and no end date are left out.
{
  const d = { days: [7, 6, 6], startTime: '01:00', durationMinutes: 240, timezone: ' ', until: '' }
  assert.deepEqual(M.recurringPayload(server, d, '  Weekend patching '), {
    scope_type: 'server', scope_server_id: 's-1', reason: 'Weekend patching',
    recurrence: { days: [6, 7], start_time: '01:00', duration_minutes: 240 },
  })
  const withTz = M.recurringPayload(device, { ...d, timezone: 'Europe/Berlin', until: '2027-01-31' }, '')
  assert.equal(withTz.scope_type, 'device')
  assert.equal(withTz.scope_device_id, 'd-1')
  assert.equal(withTz.reason, null)
  assert.deepEqual(withTz.recurrence, { days: [6, 7], start_time: '01:00', duration_minutes: 240,
    timezone: 'Europe/Berlin', until: '2027-01-31' })
  // One-off windows send exactly what they always did.
  const start = new Date('2026-11-19T10:00:00Z')
  const end = new Date('2026-11-19T11:00:00Z')
  assert.deepEqual(M.oneOffPayload(device, start, end, 'Firmware'), {
    scope_type: 'device', scope_device_id: 'd-1', starts_at: start.toISOString(), ends_at: end.toISOString(),
    reason: 'Firmware' })
}

// A listed window shows the occurrence in force, else the next one.
{
  const recurring = { id: 'w', scope_type: 'server', scope_label: 'backup-01', starts_at: '2026-11-01T01:00:00Z',
    ends_at: '2026-11-01T02:00:00Z', active: false, recurring: true, schedule: 'Every day at 02:00 for 1 h (Europe/Berlin)',
    next_starts_at: '2026-11-20T01:00:00Z', next_ends_at: '2026-11-20T02:00:00Z' }
  assert.deepEqual(M.windowTiming(recurring), { state: 'Next', from: '2026-11-20T01:00:00Z', to: '2026-11-20T02:00:00Z' })
  assert.equal(M.windowTiming({ ...recurring, active: true }).state, 'Active')
  const oneOff = { id: 'o', scope_type: 'device', scope_label: 'core-sw-01', starts_at: '2026-11-21T01:00:00Z',
    ends_at: '2026-11-21T02:00:00Z', active: false }
  assert.deepEqual(M.windowTiming(oneOff), { state: 'Upcoming', from: oneOff.starts_at, to: oneOff.ends_at })

  // Only the asset's own windows can be removed here; others say where they come from.
  assert(M.ownWindow(recurring, server) && !M.ownWindow(recurring, device))
  assert.equal(M.viaLabel(recurring, server), '')
  assert.equal(M.viaLabel({ ...oneOff, scope_type: 'device', scope_label: 'esx-01' }, server), 'via linked device (esx-01)')
  assert.equal(M.viaLabel({ ...oneOff, scope_type: 'server_tag', scope_label: 'server tag:backup' }, server),
    'via server tag:backup')
  assert.equal(M.viaLabel({ ...oneOff, scope_type: 'group', scope_label: 'Core' }, device), 'via Core')
}

// Both pages use the shared dialog; the alert rule wizard is not involved.
{
  const actions = fs.readFileSync('src/pages/device/DeviceActions.tsx', 'utf8')
  assert.match(actions, /<MaintenanceDialog[\s\S]*kind: 'device'/)
  const serverPage = fs.readFileSync('src/pages/ServerDetail.tsx', 'utf8')
  assert.match(serverPage, /<MaintenanceDialog[\s\S]*kind: 'server'/)
  assert.match(serverPage, /\/device-maintenance\/servers\/\$\{id\}/)
  const dialog = fs.readFileSync('src/components/maintenance/MaintenanceDialog.tsx', 'utf8')
  assert(!/text-\[\d+px\]/.test(dialog), 'rem type scale only')
}

console.log('maintenance windows UI model: ok')
