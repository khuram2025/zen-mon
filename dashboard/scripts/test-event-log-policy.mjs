// Agent policy event-log editor and the server Events tab. Policies stored an empty filter list, which agents read as
// "query no channel", so the Events tab of every Windows server went empty after enrolment and blamed a collector
// switch that did not exist. The editor now writes channels or [] (= the defaults) and the event_log on/off flag.
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
  stdin: { contents: `export * from './src/lib/eventLogFilters'`, resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', plugins: [aliasPlugin], logLevel: 'silent',
})
const E = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)

let r = E.parseEventLogFilters('')
assert.deepEqual(r, { filters: [] }, 'blank = the default channels')
r = E.parseEventLogFilters('System: Critical, error, error\n\nSecurity: info | 4625, 4740, 4625')
assert.deepEqual(r.filters, [
  { channel: 'System', levels: ['critical', 'error'] },
  { channel: 'Security', levels: ['information'], ids: [4625, 4740] },
])
assert.match(E.parseEventLogFilters('System').error, /Line 1/)
assert.match(E.parseEventLogFilters('System: loud').error, /not a level/)
assert.match(E.parseEventLogFilters('System: error\nsystem: warning').error, /Line 2: system is listed twice/)
assert.match(E.parseEventLogFilters('System:').error, /at least one level/)
assert.match(E.parseEventLogFilters('System: error | 0').error, /not an event ID/)

const text = E.formatEventLogFilters([{ channel: 'Security', levels: ['information'], ids: [4625] }, { channel: 'System', levels: ['error'] }])
assert.equal(text, 'Security: information | 4625\nSystem: error')
assert.deepEqual(E.parseEventLogFilters(text).filters, [{ channel: 'Security', levels: ['information'], ids: [4625] }, { channel: 'System', levels: ['error'] }])
assert.deepEqual(E.parseEventLogFilters(E.DEFAULT_EVENT_LOG_TEXT).filters.map((f) => f.channel), ['System', 'Application'])

assert.equal(E.eventLogSummary({ event_log_filters: [], feature_flags: {} }), 'Default (System, Application)')
assert.equal(E.eventLogSummary({ event_log_filters: [{ channel: 'Security' }], feature_flags: {} }), 'Security')
assert.equal(E.eventLogSummary({ event_log_filters: [], feature_flags: { event_log: false } }), 'Off')
assert.equal(E.eventLogSummary({ feature_flags: { event_log: false, collect_event_log: true } }), 'Default (System, Application)', 'the agent applies collect_event_log last')

const page = fs.readFileSync('src/pages/AgentPoliciesPage.tsx', 'utf8')
assert(page.includes('feature_flags: { ...f.other_flags, event_log: f.event_log_enabled }'), 'the switch writes the event_log flag and keeps other flags')
assert(page.includes("event_log_filters: 'filters' in parsedEventLog ? parsedEventLog.filters : []"), 'filters are sent')

const tab = fs.readFileSync('src/pages/servers/detail/tabs/EventsTab.tsx', 'utf8')
assert(tab.includes('useTimeRange()') && tab.includes('<TimeRangePicker'), 'the Events tab follows the page time range')
assert(tab.includes("isCustom ? { from: range.fromISO, to: range.toISO } : { hours: range.hours }"), 'a custom range is sent as from/to')
assert(!tab.includes('Enable the event log collector'), 'no advice to flip a switch that did not exist')
assert(!tab.includes('EVENT_WINDOWS'), 'no second, tab-only time window')

console.log('Event log policy: channel editor parses and formats filters (blank = defaults), on/off is the event_log flag, list summary; the Events tab follows the page range and explains an empty period.')
