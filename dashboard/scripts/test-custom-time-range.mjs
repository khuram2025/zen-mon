// Custom time range: a start in the future is refused instead of applied. The inputs open on today's date and show
// day/month, so changing only the day to 27 on 1 Nov asked for 27 Nov; the range applied silently and the server
// Performance tab then blamed the agent's monitoring profile for the empty chart.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const src = path.join(process.cwd(), 'src')
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
const out = await build({
  stdin: { contents: `export { checkCustomRange, toLocalInput, longDateTime } from './src/components/TimeRangePicker'`, resolveDir: process.cwd(), loader: 'tsx' },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022',
  plugins: [aliasPlugin], logLevel: 'silent',
})
const T = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)

// Inputs are local wall-clock values, so "now" is built the same way: the test holds in any timezone.
const now = new Date('2026-11-01T16:00').getTime()

let r = T.checkCustomRange('2026-11-27T15:21', '2026-11-28T16:21', now)
assert.equal(r.ok, false, 'a range starting on 27 Nov is refused on 1 Nov')
assert.equal(r.field, 'from')
assert.match(r.message, /is in the future/)
assert.match(r.message, /2026/)

r = T.checkCustomRange('2026-10-27T15:21', '2026-10-28T16:21', now)
assert.equal(r.ok, true, 'the intended October range applies')
assert.equal(r.clipped, false)
assert.equal(r.fromISO, new Date('2026-10-27T15:21').toISOString())
assert.equal(r.toISO, new Date('2026-10-28T16:21').toISOString())

r = T.checkCustomRange('2026-11-01T09:00', '2026-11-01T23:59', now)
assert.equal(r.ok, true, 'a range ending later today is allowed')
assert.equal(r.clipped, true)
assert.equal(r.toISO, new Date(now).toISOString(), 'and stops at now')

assert.equal(T.checkCustomRange('2026-11-01T16:00', '2026-11-01T17:00', now).ok, false, 'starting exactly now holds no data either')
assert.equal(T.checkCustomRange('', '', now).field, 'both')
assert.equal(T.checkCustomRange('2026-10-28T10:00', '2026-10-27T10:00', now).message, 'The end time must be after the start time.')
assert.equal(T.checkCustomRange('nope', '2026-10-27T10:00', now).field, 'from')

assert.equal(T.toLocalInput(new Date('2026-10-05T07:04').getTime()), '2026-10-05T07:04')
assert.equal(T.toLocalInput(NaN), '')
assert.match(T.longDateTime(now), /Nov/, 'the preview spells out the month')

const picker = fs.readFileSync('src/components/TimeRangePicker.tsx', 'utf8')
assert.equal((picker.match(/max=\{maxInput\}/g) || []).length, 2, 'both inputs stop at now in the calendar')
assert(picker.includes('checkCustomRange(fromInput, toInput, serverNow())'), 'Apply checks against the appliance clock')

const perf = fs.readFileSync('src/pages/servers/detail/tabs/PerformanceTab.tsx', 'utf8')
assert(perf.includes("range.future ? 'This time range is in the future'"), 'a future range says so')
const customHint = perf.indexOf('No metrics were stored for this period')
const agentHint = perf.indexOf('The agent is online but is not reporting server metrics')
assert(customHint > 0 && agentHint > customHint, 'an empty custom range does not blame the agent')

console.log('Custom time range: future starts refused with the date spelled out, ends cut to now, the October range applies; the server Performance tab explains an empty future or past custom range instead of blaming the agent.')
