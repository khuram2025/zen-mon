import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const compiled = await transformWithEsbuild(fs.readFileSync('src/pages/device/eventsModel.ts', 'utf8'), 'eventsModel.ts', { loader: 'ts', format: 'esm' })
const m = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// Alert messages: the legacy "[ZenPlus SEVERITY] " transport prefix is dropped, nothing else.
assert.equal(m.stripAlertPrefix('[ZenPlus CRITICAL] Device core-sw1 is down'), 'Device core-sw1 is down')
assert.equal(m.stripAlertPrefix('[ZenPlus WARNING]CPU high'), 'CPU high')
assert.equal(m.stripAlertPrefix('CPU [ZenPlus WARNING] high'), 'CPU [ZenPlus WARNING] high')
assert.equal(m.stripAlertPrefix(null), '')
assert.equal(m.alertSeverity('critical'), 'critical')
assert.equal(m.alertSeverity('WARNING'), 'warning')
assert.equal(m.alertSeverity('info'), 'info')
assert.equal(m.alertSeverity(undefined), 'info')

// Naive backend timestamps are UTC.
assert.equal(m.eventTime('2026-10-18 10:00:00'), Date.parse('2026-10-18T10:00:00Z'))
assert.equal(m.eventTime('2026-10-18T10:00:00+02:00'), Date.parse('2026-10-18T08:00:00Z'))
for (const bad of [null, undefined, '', 'nope']) assert.equal(m.eventTime(bad), null)

const alerts = m.alertEvents([
  { id: 'a1', status: 'active', severity: 'critical', message: '[ZenPlus CRITICAL] Device down', rule_name: 'Device down rule', triggered_at: '2026-10-18T10:05:00Z' },
  { id: 'a2', status: 'resolved', severity: 'warning', message: '', rule_name: 'High CPU', triggered_at: '2026-10-18T10:20:00Z' },
  { id: 'a3', status: 'active', severity: 'warning', message: 'no time', triggered_at: null },
])
assert.equal(alerts.length, 2, 'an alert without a time is not placed on the timeline')
assert.deepEqual(alerts.map(a => [a.title, a.status, a.severity, a.href]), [
  ['Device down', 'active', 'critical', '/alerts/a1'],
  ['High CPU', 'resolved', 'warning', '/alerts/a2'],
])
assert.equal(alerts[0].subtitle, 'Rule: Device down rule')
assert.equal(alerts[1].subtitle, undefined, 'the rule name is not repeated when it is the title')

// Maintenance: active and upcoming windows, labelled.
const maint = m.maintenanceEvents({
  active: [{ id: 'm1', starts_at: '2026-10-18T09:00:00Z', ends_at: '2026-10-18T12:00:00Z', reason: 'Firmware', active: true }],
  upcoming: [{ id: 'm2', starts_at: '2026-10-20T01:00:00Z', ends_at: '2026-10-20T02:00:00Z' }],
})
assert.deepEqual(maint.map(e => [e.kind, e.tag, e.title]), [
  ['maintenance', 'In progress', 'Maintenance in progress'],
  ['maintenance', 'Upcoming', 'Maintenance scheduled'],
])
assert.match(maint[0].subtitle, /Firmware/)
assert.equal(maint[0].endsAt, '2026-10-18T12:00:00Z')
assert.deepEqual(m.maintenanceEvents(undefined), [])

// Traps from the "last N hours" endpoint are cut to the selected window.
const from = Date.parse('2026-10-18T10:00:00Z'), to = from + 3600_000
const traps = [{ timestamp: '2026-10-18 09:59:59' }, { timestamp: '2026-10-18 10:00:00' }, { timestamp: '2026-10-18T10:30:00Z' }, { timestamp: '2026-10-18T11:00:01Z' }, { timestamp: null }]
assert.deepEqual(m.trapsInRange(traps, from, to).map(t => t.timestamp), ['2026-10-18 10:00:00', '2026-10-18T10:30:00Z'])

// Activity rows keep their kind; unknown kinds read as status rows.
const activity = m.activityToTimeline([
  { id: 'status-1', timestamp: '2026-10-18T10:10:00Z', severity: 'critical', title: 'Device went down', kind: 'status' },
  { id: 'flap-start-1', timestamp: '2026-10-18T10:11:00Z', severity: 'warning', title: 'Flapping started', kind: 'flapping' },
  { id: 'trap-1', timestamp: '2026-10-18T10:30:00Z', severity: 'info', title: 'linkDown', kind: 'trap' },
  { id: 'reboot-1', timestamp: '2026-10-18T10:40:00Z', severity: 'warning', title: 'Device reboot detected', kind: 'reboot' },
  { id: 'x', timestamp: '2026-10-18T10:41:00Z', severity: 'success', title: 'Device recovered' },
  { id: 'bad', timestamp: 'never', severity: 'info', title: 'Dropped' },
])
assert.deepEqual(activity.map(e => e.kind), ['status', 'flapping', 'trap', 'reboot', 'status'])

// Merge: newest first, duplicate ids once.
const merged = m.mergeEvents(alerts, activity, maint, [alerts[0]])
assert.equal(merged.length, alerts.length + activity.length + maint.length)
assert.deepEqual(merged.map(e => e.id).slice(0, 3), ['maint-m2', 'x', 'reboot-1'])
for (let i = 1; i < merged.length; i++) assert(merged[i - 1].ts >= merged[i].ts, 'sorted newest first')

// Filters: type, severity and text combine; counts show what selecting a chip would show.
const all = m.EMPTY_FILTER
assert.equal(m.filterEvents(merged, all).length, merged.length)
assert.deepEqual(m.filterEvents(merged, { ...all, kinds: ['alert'] }).map(e => e.id), ['alert-a2', 'alert-a1'])
assert.deepEqual(m.filterEvents(merged, { ...all, kinds: ['alert', 'trap'], severities: ['critical'] }).map(e => e.id), ['alert-a1'])
assert.deepEqual(m.filterEvents(merged, { ...all, q: '  FIRMWARE ' }).map(e => e.id), ['maint-m1'])
assert.deepEqual(m.filterEvents(merged, { ...all, q: 'resolved' }).map(e => e.id), ['alert-a2'], 'alert status is searchable')
assert.equal(m.filterEvents(merged, { ...all, q: 'nothing like this' }).length, 0)
const counts = m.chipCounts(merged, { kinds: ['alert'], severities: ['warning'], q: '' })
assert.equal(counts.kinds.alert, 1, 'type counts respect the severity filter')
assert.equal(counts.kinds.flapping, 1)
assert.equal(counts.all, 3, 'All counts every type under the other filters (three warnings)')
assert.equal(counts.severities.critical, 1, 'severity counts respect the type filter')
assert.equal(counts.severities.warning, 1)

// URL state: etype / esev / eq, other params preserved, empty values removed, unknown values ignored.
const params = new URLSearchParams('tab=events&range=24h&etype=alert,bogus,trap&esev=critical&eq=link')
assert.deepEqual(m.parseEventFilter(params), { kinds: ['alert', 'trap'], severities: ['critical'], q: 'link' })
assert.deepEqual(m.parseEventFilter(new URLSearchParams('tab=events')), { kinds: [], severities: [], q: '' })
const written = m.writeEventFilter(params, { kinds: ['trap', 'status'], severities: [], q: '' })
assert.equal(written.get('tab'), 'events')
assert.equal(written.get('range'), '24h')
assert.equal(written.get('etype'), 'status,trap', 'canonical order')
assert.equal(written.has('esev'), false)
assert.equal(written.has('eq'), false)
assert.equal(params.get('etype'), 'alert,bogus,trap', 'the input is not mutated')
assert.deepEqual(m.toggleValue(['a'], 'b'), ['a', 'b'])
assert.deepEqual(m.toggleValue(['a', 'b'], 'a'), ['b'])

// Mini chart buckets: round widths across the range, stacked by severity, outside events not counted.
assert.equal(m.bucketWidth(from, to), 60_000, '1h → 1-minute buckets')
assert.equal(m.bucketWidth(from, from + 24 * 3600_000), 30 * 60_000, '24h → 30-minute buckets')
assert.equal(m.bucketWidth(from, from + 7 * 24 * 3600_000), 3 * 3600_000, '7d → 3-hour buckets')
assert.equal(m.bucketWidth(from, from + 30 * 24 * 3600_000), 12 * 3600_000, '30d → 12-hour buckets')
const buckets = m.bucketEvents(merged, from, to)
assert.equal(buckets.length, 60)
assert.equal(buckets[0].start, from)
assert.equal(buckets[59].end, to)
const total = buckets.reduce((s, b) => s + b.critical + b.warning + b.info + b.success, 0)
assert.equal(total, m.countInRange(merged, from, to))
assert.equal(total, merged.length - 2, 'maintenance windows starting outside the range are listed but not charted')
assert.equal(buckets[5].critical, 1, 'alert at 10:05 is critical in the 6th minute')
assert.equal(buckets[10].critical, 1, 'status change at 10:10')
assert.equal(buckets[11].warning, 1, 'flapping start at 10:11')
const edge = m.bucketEvents([{ ...alerts[0], ts: to }], from, to)
assert.equal(edge[59].critical, 1, 'an event at the range end falls in the last bucket')
assert.deepEqual(m.bucketEvents(merged, to, from), [])
assert.deepEqual(m.bucketEvents([], NaN, to), [])
assert.equal(m.EVENTS_PAGE_SIZE, 50)
console.log('Device events model passed: prefix stripping, UTC parsing, source mapping, trap windowing, merge order, filters, chip counts, URL state and severity buckets.')

// Wiring: the tab builds the full log without collapsing flap episodes and keeps each source independent.
const tab = fs.readFileSync('src/pages/device/EventsTab.tsx', 'utf8')
assert(tab.includes('buildActivityEvents(traps || [], statusHistory || [], metrics || {}, range.fromISO, range.toISO, episodes || [], false)'), 'full log keeps every change')
assert(tab.includes("useCan()('alerts.view')"), 'alerts are requested only with alerts.view')
assert(tab.includes('/alerts?device_id=${id}&from=${fromQ}&to=${toQ}&limit=${ALERT_LIMIT}'))
assert(tab.includes('/traps?hours=${trapHours}&limit=${TRAP_LIMIT}'))
assert(tab.includes('Math.min(720, Math.max(1, Math.ceil(range.hours)))'))
assert(tab.includes('refetchInterval: range.isCustom ? (false as const) : 30_000'), 'custom ranges are not polled')
assert(tab.includes('placeholderData: keepPreviousData'))
assert(tab.includes('could not be loaded'), 'a failed source is named')
assert(tab.includes('{ replace: true }'), 'filter changes do not add history entries')
assert(tab.includes('No events in {rangePhrase(range.label)}'), 'the empty state names the range')
assert(!/text-\[(9|10|11)px\]/.test(tab), 'labels are at least 12 px')
assert(!/#[0-9a-fA-F]{6}\b/.test(tab), 'theme tokens only')
console.log('Events tab wiring passed: full flap log, permission-gated alerts, range-bound sources, polling rules, per-source failures and URL filters.')
