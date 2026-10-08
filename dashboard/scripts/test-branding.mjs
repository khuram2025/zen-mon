// Branding formatters must print exactly what the server renderers print
// (server/app/services/branding.py, pinned by server/tests/test_branding.py).
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const compiled = await transformWithEsbuild(fs.readFileSync('src/lib/branding.ts', 'utf8'), 'branding.ts', { loader: 'ts', format: 'esm' })
const b = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

const TS = '2026-10-07T11:05:09Z'
const fmt = (o) => ({ timezone: 'UTC', date_format: 'YYYY-MM-DD', time_format: '24h', ...o })

for (const { value, example } of b.DATE_FORMATS) {
  assert.equal(b.formatDate(TS, fmt({ date_format: value })), example, value)
}

const riyadh = fmt({ timezone: 'Asia/Riyadh' })
assert.equal(b.formatTime(TS, riyadh), '14:05')
assert.equal(b.zoneLabel('Asia/Riyadh', TS), 'GMT+3')
// Naive API timestamps are UTC.
assert.equal(b.formatDateTime('2026-10-07T11:05:09', riyadh), '2026-10-07 14:05')
assert.equal(b.formatDateTime(TS, riyadh, { zone: true }), '2026-10-07 14:05 GMT+3')

const ny = fmt({ timezone: 'America/New_York', time_format: '12h', date_format: 'MM/DD/YYYY' })
assert.equal(b.formatTime(TS, ny), '7:05 AM')
assert.equal(b.formatTime(TS, ny, true), '7:05:09 AM')
assert.equal(b.zoneLabel('America/New_York', TS), 'GMT-4')
assert.equal(b.zoneLabel('America/New_York', '2026-01-07T00:00:00Z'), 'GMT-5')
assert.equal(b.formatPeriod(TS, '2026-10-07T23:05:09Z', ny), '10/07/2026 7:05 AM — 10/07/2026 7:05 PM GMT-4')
assert.equal(b.zoneLabel('Asia/Kolkata', TS), 'GMT+5:30')
assert.equal(b.zoneLabel('Asia/Kathmandu', TS), 'GMT+5:45')
assert.equal(b.zoneLabel('Europe/London', '2026-01-07T00:00:00Z'), 'GMT')
assert.equal(b.zoneLabel('UTC', TS), 'UTC')
assert.equal(b.zoneLabel('Not/AZone', TS), 'UTC')
// Midnight edge: 12h clock shows 12, never 0.
assert.equal(b.formatTime('2026-10-07T21:00:00Z', riyadh), '00:00')
assert.equal(b.formatTime('2026-10-07T21:00:00Z', { ...riyadh, time_format: '12h' }), '12:00 AM')
assert.equal(b.formatTime('2026-10-07T09:00:00Z', { ...riyadh, time_format: '12h' }), '12:00 PM')

assert.ok(b.timezoneList().includes('UTC'))
assert.ok(b.timezoneList().includes('Asia/Riyadh'))
assert.equal(b.logoUrl('dark', 'v1', false), '/api/v1/settings/company/logo?variant=dark&fallback=false&v=v1')
assert.equal(b.onColor('#FFFFFF'), '#0f172a')
assert.equal(b.onColor('#4F6BF6'), '#ffffff')
assert.equal(b.onColor('bad'), '#ffffff')

console.log('branding formatters ok')
