import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/components/devices/availability.ts', 'utf8')
const compiled = await transformWithEsbuild(src, 'availability.ts', { loader: 'ts', format: 'esm' })
const { formatDeviceAvailability: format, deviceAvailabilityBuckets: buckets } = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
assert.equal(format(99.96), '99.96%')
assert.equal(format(100), '100.00%')
assert.equal(format(0), '0.00%')
for (const value of [null, undefined, NaN, Infinity]) assert.equal(format(value), '—')
const from = Date.parse('2026-09-01T00:00:00Z'), to = from + 3600000
const point = (offset, pct, count, legacy = true) => ({ timestamp: new Date(from + offset).toISOString(), is_up: legacy, uptime_pct: pct, sample_count: count })
const result = buckets([point(0, 99.96, 40000)], from, to)
assert.equal(result[0].state, 'partial')
assert.equal(Math.round(result[0].down), 16)
assert.equal(format(result[0].pct), '99.96%')
assert(result.slice(1).every(b => b.state === 'gap'))
const weighted = buckets([point(0, 100, 99), point(1, 0, 1, false)], from, to)[0]
assert.equal(weighted.pct, 99)
assert.equal(weighted.down, 1)
assert.equal(weighted.state, 'partial')
assert.equal(buckets([point(0, 0, 10, false)], from, to)[0].state, 'down')
assert.equal(buckets([point(0, null, 0, null)], from, to)[0].state, 'gap')
assert.equal(buckets([{timestamp: new Date(from).toISOString(), is_up: false}], from, to)[0].state, 'down')
assert(buckets([point(-1, 0, 10), point(3600001, 0, 10), {timestamp:'invalid', is_up:false}], from, to).every(b => b.state === 'gap'))
const page = fs.readFileSync('src/pages/DeviceDetailPage.tsx', 'utf8')
const section = page.slice(page.indexOf('function AvailabilityTimelineCard('), page.indexOf('function ActivityLogCard('))
assert(section.includes('const pct = availabilityPct ?? null'))
assert(!section.includes('upCount / total'))
assert(page.includes('availabilityPct={availabilityPct}'))
assert(page.includes('const availabilityLabel = formatDeviceAvailability(availabilityPct)'))
console.log('Device availability regressions passed: 99.96% precision, partial failures, sample weighting, missing data, range bounds, and shared headline source.')
