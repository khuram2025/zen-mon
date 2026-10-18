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

// Repeated alerts (D-09): key and label replay the vectors the server's device_insights replays too.
const vectors = JSON.parse(fs.readFileSync('../scripts/dev/device-alert-groups/vectors.json', 'utf8'))
for (const c of vectors.key_cases) assert.equal(m.alertGroupKey(c.rule_id, c.rule_name, c.metadata), c.expect, `key: ${c.name}`)
for (const c of vectors.label_cases) assert.equal(m.alertGroupLabel(c.titles, c.rule_name, c.metadata), c.expect, `label: ${c.name}`)
assert.equal(m.spanPhrase(0), '1 min')
assert.equal(m.spanPhrase(12 * 60_000 + 1), '13 min')
assert.equal(m.spanPhrase(3600_000), '1 hour')
assert.equal(m.spanPhrase(5 * 3600_000), '5 hours')
assert.equal(m.spanPhrase(2 * 86400_000 + 1), '3 days', 'rounded up')
assert.equal(m.repeatPhrase(5, 0, 2.5 * 86400_000), '×5 in 3 days')

const H = 3600_000, T0 = Date.parse('2026-10-18T12:00:00Z')
const at = (hoursAgo) => new Date(T0 - hoursAgo * H).toISOString()
const lossMeta = (pct) => ({ condition: `Packet loss ${pct}% (limit 10%)`, condition_metric: 'packet_loss', new_status: 'degraded' })
const repeated = m.alertEvents([
  { id: 'p1', status: 'resolved', severity: 'warning', rule_id: 'r-1', rule_name: 'High Latency', triggered_at: at(60), metadata: lossMeta(33) },
  { id: 'p2', status: 'resolved', severity: 'critical', rule_id: 'r-1', rule_name: 'High Latency', triggered_at: at(30), metadata: lossMeta(52) },
  { id: 'p3', status: 'active', severity: 'warning', rule_id: 'r-1', rule_name: 'High Latency', triggered_at: at(1), metadata: lossMeta(41) },
  { id: 'l1', status: 'resolved', severity: 'warning', rule_id: 'r-1', rule_name: 'High Latency', triggered_at: at(20),
    metadata: { condition: 'Latency 412 ms (limit 100 ms)', condition_metric: 'rtt', new_status: 'degraded' } },
  { id: 'd1', status: 'resolved', severity: 'critical', rule_id: 'r-2', rule_name: 'Device Down', triggered_at: at(10),
    metadata: { condition: 'No response', condition_metric: 'reachability', new_status: 'down' } },
  { id: 'd2', status: 'acknowledged', severity: 'critical', rule_id: 'r-2', rule_name: 'Device Down', triggered_at: at(5),
    metadata: { condition: 'No response', condition_metric: 'reachability', new_status: 'down' } },
  { id: 'u1', status: 'resolved', severity: 'critical', rule_id: 'r-2', rule_name: 'Device Down', triggered_at: at(9),
    metadata: { condition: 'Responding again', condition_metric: 'reachability', new_status: 'up', is_recovery: true } },
  { id: 'g1', status: 'resolved', severity: 'warning', rule_name: 'High Latency', triggered_at: at(50), metadata: { new_status: 'degraded' },
    message: 'High Latency: fw-01 (10.0.0.1) is DEGRADED' },
  { id: 'g2', status: 'resolved', severity: 'warning', rule_name: 'High Latency', triggered_at: at(40), metadata: { new_status: 'degraded' },
    message: 'High Latency: fw-01 (10.0.0.1) is DEGRADED' },
  { id: 'n1', status: 'active', severity: 'info', triggered_at: at(3), message: 'Unruled' },
  { id: 'n2', status: 'active', severity: 'info', triggered_at: at(4), message: 'Unruled' },
], { hostname: 'fw-01', ip_address: '10.0.0.1' })
assert.equal(repeated.find(e => e.id === 'alert-p1').group.key, 'r-1|packet_loss|degraded|')
assert.equal(repeated.find(e => e.id === 'alert-n1').group, undefined, 'no rule: no group key')
const status = m.activityToTimeline([{ id: 'status-x', timestamp: at(30), severity: 'warning', title: 'Device degraded', kind: 'status' }])
const timeline = m.mergeEvents(repeated, status)
const rows = m.groupEvents(timeline)
const byId = Object.fromEntries(rows.map(r => [r.id, r]))
assert.deepEqual(rows.map(r => r.id), [
  'group-r-1|packet_loss|degraded|', 'alert-n1', 'alert-n2', 'group-r-2|reachability|down|', 'alert-u1',
  'alert-l1', 'status-x', 'group-High Latency||degraded|',
], 'a group takes its newest alert’s place; other events and alerts without a rule stay as they are')
const loss = byId['group-r-1|packet_loss|degraded|']
assert(m.isGroup(loss) && !m.isGroup(byId['alert-l1']))
assert.equal(loss.title, 'Degraded (packet loss) ×3 in 3 days')
assert.equal(loss.label, 'Degraded (packet loss)')
assert.deepEqual(loss.members.map(e => e.id), ['alert-p3', 'alert-p2', 'alert-p1'], 'members newest first')
assert.deepEqual(loss.members.map(e => e.href), ['/alerts/p3', '/alerts/p2', '/alerts/p1'], 'every alert stays reachable')
assert.equal(loss.severity, 'critical', 'the worst member')
assert.equal(loss.status, 'active', 'the most urgent workflow state')
assert.equal(loss.open, 1)
assert.equal(loss.ts, Date.parse(at(1)))
assert.equal(loss.firstTs, Date.parse(at(60)))
assert.equal(loss.href, undefined, 'the group row expands; its alerts link')
assert.equal(loss.subtitle, 'Latest: Packet loss 41% (limit 10%) · Rule: High Latency · 1 open')
const down = byId['group-r-2|reachability|down|']
assert.equal(down.title, 'No response ×2 in 5 hours', 'one shared title is the label')
assert.equal(down.status, 'acknowledged')
assert.equal(byId['alert-u1'].title, 'Responding again', 'a recovery is not grouped with the outage')
assert.equal(byId['group-High Latency||degraded|'].title, 'High Latency · Degraded ×2 in 10 hours', 'legacy rows: rule name and new status')
assert.equal(byId['group-High Latency||degraded|'].status, 'resolved')
assert.equal(m.groupEvents(timeline, false), timeline, 'grouping off: the plain list')
assert.equal(m.groupEvents([]).length, 0)

// Filters apply to each alert before grouping: a group holds only what the filters show, and a lone match is a plain row.
const critical = m.groupEvents(m.filterEvents(timeline, { ...m.EMPTY_FILTER, severities: ['critical'] }))
assert.deepEqual(critical.map(r => r.id), ['group-r-2|reachability|down|', 'alert-u1', 'alert-p2'])
const warnLoss = m.groupEvents(m.filterEvents(timeline, { ...m.EMPTY_FILTER, severities: ['warning'], q: 'packet loss' }))
assert.deepEqual(warnLoss.map(r => r.id), ['group-r-1|packet_loss|degraded|'])
assert.equal(warnLoss[0].title, 'Degraded (packet loss) ×2 in 3 days')
assert.equal(warnLoss[0].severity, 'warning', 'severity from the alerts the filter shows')
const counts2 = m.chipCounts(timeline, m.EMPTY_FILTER)
assert.equal(counts2.kinds.alert, 11, 'chip counts still count every alert')
assert.equal(m.countInRange(timeline, T0 - 72 * H, T0), 12, 'the chart still counts every alert')

// Paging counts rows: 120 repeats of one condition and 60 other alerts are 61 rows, two pages.
const many = m.mergeEvents(m.alertEvents([
  ...Array.from({ length: 120 }, (_, i) => ({ id: `r${i}`, status: 'resolved', severity: 'warning', rule_id: 'r-9', rule_name: 'Loss',
    triggered_at: new Date(T0 - i * 60_000).toISOString(), metadata: lossMeta(20 + (i % 5)) })),
  ...Array.from({ length: 60 }, (_, i) => ({ id: `o${i}`, status: 'resolved', severity: 'info', rule_id: `x-${i}`, rule_name: `Rule ${i}`,
    triggered_at: new Date(T0 - (i * 2 + 1) * 60_000).toISOString(), message: `Other ${i}` })),
]))
const pageRows = m.groupEvents(many)
assert.equal(pageRows.length, 61)
assert.equal(pageRows[0].members.length, 120)
assert.equal(pageRows.slice(0, m.EVENTS_PAGE_SIZE).length, 50)

// URL: grouping is on by default; egroup=off turns it off, other params are kept.
assert.equal(m.groupingEnabled(new URLSearchParams('tab=events')), true)
assert.equal(m.groupingEnabled(new URLSearchParams('egroup=off')), false)
const off = m.writeGrouping(new URLSearchParams('tab=events&etype=alert'), false)
assert.equal(off.toString(), 'tab=events&etype=alert&egroup=off')
assert.equal(m.writeGrouping(off, true).toString(), 'tab=events&etype=alert')
assert.equal(m.ruleOpenAlertsHref('d 1', 'r-1'), '/alerts?device_id=d%201&rule_id=r-1&view=open')
console.log('Repeated alerts passed: shared key/label vectors, span wording, grouping order, members and links, filters before grouping, row paging and URL state.')

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
assert(tab.includes('groupEvents(filtered, grouped)'), 'grouping runs on the filtered alerts')
assert(tab.includes('items.slice(0, shown)'), 'paging counts grouped rows')
assert(tab.includes('aria-expanded={open}'), 'a group expands to its alerts')
assert(tab.includes('writeGrouping(prev, !grouped), { replace: true }'), 'the grouping toggle lives in the URL')
assert(tab.includes('${grouped}|${range.fromISO}'), 'paging resets when grouping or the range changes')
const summaryTab = fs.readFileSync('src/pages/device/SummaryTab.tsx', 'utf8')
assert(summaryTab.includes('summary.alerts?.groups'), 'Summary recent events use the grouped open alerts')
assert(summaryTab.includes('`/alerts/${m.id}`'), 'grouped Summary rows list every alert with its link')
const issuesBar = fs.readFileSync('src/pages/device/IssuesBar.tsx', 'utf8')
assert(issuesBar.includes('repeatPhrase(issue.count!, first, last)'), 'repeated alert issues show their count and span')
const page = fs.readFileSync('src/pages/DeviceDetailPage.tsx', 'utf8')
assert(page.includes('ruleOpenAlertsHref(id, issue.rule_id)'), 'a repeated alert issue opens the rule’s open alerts')
console.log('Events tab wiring passed: full flap log, permission-gated alerts, range-bound sources, polling rules, per-source failures, URL filters and grouped repeats.')
