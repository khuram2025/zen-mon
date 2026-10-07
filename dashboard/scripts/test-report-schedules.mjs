// Report schedule helpers (features/reports/schedules/model.ts): frequency wording,
// next-run calculation in the organisation timezone, period defaults, relative
// times in both directions, recipients, validation and the request body.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const toUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const compile = async (path, replace = {}) => {
  let src = fs.readFileSync(path, 'utf8')
  for (const [from, to] of Object.entries(replace)) src = src.split(from).join(to)
  return toUrl((await transformWithEsbuild(src, path, { loader: 'ts', format: 'esm' })).code)
}
const bURL = await compile('src/lib/branding.ts')
const pURL = await compile('src/features/reports/periods.ts', { "from '@/lib/branding'": `from '${bURL}'` })
const mURL = await compile('src/features/reports/schedules/model.ts', {
  "from '@/lib/branding'": `from '${bURL}'`,
  "from '../periods'": `from '${pURL}'`,
})
const m = await import(mURL)

const iso = (ds) => ds.map((d) => d.toISOString())
const RIYADH = 'Asia/Riyadh' // UTC+3, no DST
const now = new Date('2026-10-07T11:05:00Z') // Wednesday 14:05 in Riyadh

/* Frequency wording ------------------------------------------------------- */
assert.equal(m.frequencyText({ frequency: 'daily', hour: 8, minute: 0 }), 'Daily at 08:00')
assert.equal(m.frequencyText({ frequency: 'weekly', hour: 8, minute: 30, day_of_week: 1 }), 'Weekly on Monday at 08:30')
assert.equal(m.frequencyText({ frequency: 'weekly', hour: 17, minute: 45, day_of_week: 7 }), 'Weekly on Sunday at 17:45')
assert.equal(m.frequencyText({ frequency: 'monthly', hour: 8, minute: 0, day_of_month: 1 }), 'Monthly on day 1 at 08:00')
assert.equal(m.frequencyText({ frequency: 'daily', hour: 0, minute: 15 }, '12h'), 'Daily at 12:15 AM')
assert.equal(m.frequencyText({ frequency: 'daily', hour: 13, minute: 0 }, '12h'), 'Daily at 1:00 PM')
assert.equal(m.TIME_STEPS.length, 96)
assert.equal(m.TIME_STEPS[0].value, '00:00')
assert.equal(m.TIME_STEPS[95].value, '23:45')
assert.deepEqual(m.weekdaysFrom('sunday').map((d) => d.short), ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
assert.deepEqual(m.weekdaysFrom('monday').map((d) => d.id), [1, 2, 3, 4, 5, 6, 7])
assert.equal(m.ordinal(1), '1st')
assert.equal(m.ordinal(22), '22nd')
assert.equal(m.ordinal(13), '13th')

/* Period defaults ---------------------------------------------------------- */
assert.equal(m.suggestedPeriod('daily'), 'yesterday')
assert.equal(m.suggestedPeriod('weekly'), 'previous_week')
assert.equal(m.suggestedPeriod('monthly'), 'previous_month')
assert.equal(m.periodLabel('previous_month'), 'Previous month')
assert.equal(m.formatLabel('none'), 'Summary only')
assert.deepEqual(m.formatsForEngine('legacy'), ['pdf', 'excel', 'csv', 'none'])
assert.deepEqual(m.formatsForEngine('sections'), ['pdf', 'excel', 'html', 'none'])

/* Next runs (mirror report_scheduler.compute_next_run) --------------------- */
// Daily 08:00 after 14:05: tomorrow onwards.
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 8, minute: 0 }, RIYADH, 3, now)),
  ['2026-10-08T05:00:00.000Z', '2026-10-09T05:00:00.000Z', '2026-10-10T05:00:00.000Z'])
// Daily 18:30, still ahead today.
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 18, minute: 30 }, RIYADH, 1, now)), ['2026-10-07T15:30:00.000Z'])
// Exactly now is not "next".
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 14, minute: 5 }, RIYADH, 1, now)), ['2026-10-08T11:05:00.000Z'])
// Weekly Monday 08:00 from a Wednesday.
assert.deepEqual(iso(m.nextRuns({ frequency: 'weekly', hour: 8, minute: 0, day_of_week: 1 }, RIYADH, 3, now)),
  ['2026-10-12T05:00:00.000Z', '2026-10-19T05:00:00.000Z', '2026-10-26T05:00:00.000Z'])
// Weekly Wednesday: later today counts, earlier today does not.
assert.deepEqual(iso(m.nextRuns({ frequency: 'weekly', hour: 20, minute: 0, day_of_week: 3 }, RIYADH, 1, now)), ['2026-10-07T17:00:00.000Z'])
assert.deepEqual(iso(m.nextRuns({ frequency: 'weekly', hour: 9, minute: 0, day_of_week: 3 }, RIYADH, 1, now)), ['2026-10-14T06:00:00.000Z'])
// Monthly on day 1.
assert.deepEqual(iso(m.nextRuns({ frequency: 'monthly', hour: 8, minute: 0, day_of_month: 1 }, RIYADH, 3, now)),
  ['2026-11-01T05:00:00.000Z', '2026-12-01T05:00:00.000Z', '2027-01-01T05:00:00.000Z'])
// A stored day 31 runs on the last day of shorter months (server clamps the same way).
assert.deepEqual(iso(m.nextRuns({ frequency: 'monthly', hour: 8, minute: 0, day_of_month: 31 }, RIYADH, 3, now)),
  ['2026-10-31T05:00:00.000Z', '2026-11-30T05:00:00.000Z', '2026-12-31T05:00:00.000Z'])
// Local midnight crossing: 01:00 Riyadh is 22:00 UTC the day before.
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 1, minute: 0 }, RIYADH, 1, now)), ['2026-10-07T22:00:00.000Z'])
// DST: New York leaves daylight time on 2026-11-01; 08:00 local moves from 12:00Z to 13:00Z.
const nyNow = new Date('2026-10-31T12:00:00Z') // exactly 08:00 EDT
const ny = m.nextRuns({ frequency: 'daily', hour: 8, minute: 0 }, 'America/New_York', 2, nyNow)
assert.deepEqual(iso(ny), ['2026-11-01T13:00:00.000Z', '2026-11-02T13:00:00.000Z'])
assert.equal(m.crossesClockChange('America/New_York', ny[0], nyNow), true)
assert.equal(m.crossesClockChange(RIYADH, now, new Date('2027-03-01T00:00:00Z')), false)
// UTC fallback for an unknown zone.
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 8, minute: 0 }, 'Not/AZone', 1, now)), ['2026-10-08T08:00:00.000Z'])

/* Relative times: future is never "just now" -------------------------------- */
const t = now.getTime()
const H = 3600_000
assert.equal(m.relativeTo(new Date(t + 3 * H), t), 'in 3 h')
assert.equal(m.relativeTo(new Date(t + 3 * H + 40 * 60_000), t), 'in 3 h')
assert.equal(m.relativeTo(new Date(t + 2 * H + 15 * 60_000), t), 'in 2 h 15 min')
assert.equal(m.relativeTo(new Date(t + 25 * 60_000), t), 'in 25 min')
assert.equal(m.relativeTo(new Date(t + 30_000), t), 'in under a minute')
assert.equal(m.relativeTo(new Date(t - 30_000), t), 'just now')
assert.equal(m.relativeTo(new Date(t - 3 * H), t), '3 h ago')
assert.equal(m.relativeTo(new Date(t + 4 * 24 * H), t), 'in 4 d')
assert.equal(m.relativeTo(new Date(t + 90 * 24 * H), t), 'in 3 mo')
assert.equal(m.relativeTo('2026-10-07T14:05:00', t), 'in 3 h') // naive API time is UTC
assert.equal(m.relativeTo('2026-10-07T14:05:00+00:00', t), 'in 3 h')
assert.equal(m.relativeTo(null, t), '—')

/* Formatting in the organisation's formats ---------------------------------- */
const branding = { timezone: RIYADH, date_format: 'DD-MMM-YYYY', time_format: '24h', week_start: 'monday' }
assert.equal(m.runLabel(new Date('2026-10-12T05:00:00Z'), branding), 'Mon 12-Oct-2026 08:00')
assert.equal(m.coverageText('previous_month', new Date('2026-11-01T05:00:00Z'), branding),
  '01-Oct-2026 – 31-Oct-2026, whole days (GMT+3)')
assert.equal(m.coverageText('yesterday', new Date('2026-10-08T05:00:00Z'), branding), '07-Oct-2026, whole day (GMT+3)')
assert.equal(m.coverageText('previous_week', new Date('2026-10-12T05:00:00Z'), branding),
  '05-Oct-2026 – 11-Oct-2026, whole days (GMT+3)')
assert.equal(m.coverageText('last_24h', new Date('2026-10-08T05:00:00Z'), branding),
  '07-Oct-2026 08:00 – 08-Oct-2026 08:00 GMT+3')
assert.equal(m.coverageText('today', new Date('2026-10-08T05:00:00Z'), branding), '08-Oct-2026 00:00 – 08:00 GMT+3')

/* Recipients --------------------------------------------------------------- */
let r = m.addRecipients([], 'a@example.com, B@Example.com; <c@example.org>  mailto:d@example.net')
assert.deepEqual(r.next, ['a@example.com', 'B@Example.com', 'c@example.org', 'd@example.net'])
r = m.addRecipients(['a@example.com'], 'A@EXAMPLE.COM not-an-email x@y')
assert.deepEqual(r.next, ['a@example.com'])
assert.deepEqual(r.duplicates, ['A@EXAMPLE.COM'])
assert.deepEqual(r.invalid, ['not-an-email', 'x@y'])
const many = Array.from({ length: 50 }, (_, i) => `u${i}@example.com`)
r = m.addRecipients(many, 'extra@example.com')
assert.equal(r.next.length, 50)
assert.deepEqual(r.overflow, ['extra@example.com'])
assert.equal(m.isEmail('noc@zen.example'), true)
assert.equal(m.isEmail('noc@zen'), false)

/* Templates ---------------------------------------------------------------- */
const catalog = {
  categories: [{ id: 'executive', title: 'Executive' }, { id: 'availability', title: 'Availability & SLA' }],
  types: [
    { key: 'availability', title: 'Availability & SLA', description: 'SLA', category: 'availability', engine: 'sections', formats: [], filterable: ['devices'] },
    { key: 'executive_summary', title: 'Executive Summary', description: 'Legacy', category: 'executive', engine: 'legacy', formats: [] },
    { key: 'apm_performance', title: 'Application Performance', description: '', category: 'availability', engine: 'sections', formats: [], available: false },
  ],
  sections: [],
  custom: [{ id: 'u1', name: 'NOC weekly', description: '', sections: [], entries: [], category: null, tags: [], visibility: 'shared', owner_name: 'Ana' }],
  periods: [],
  can: { manage: true, export: true, share: true },
}
const opts = m.templateOptions(catalog)
assert.equal(opts.length, 4)
assert.equal(opts.find((o) => o.value === 'custom:u1').report_type, 'custom')
assert.equal(opts.find((o) => o.value === 'executive_summary').engine, 'legacy')
const groups = m.groupTemplateOptions(opts.filter((o) => o.available), catalog)
assert.deepEqual(groups.map((g) => g.title), ['Executive', 'Availability & SLA', 'Organisation templates'])
assert.deepEqual(m.searchTemplateOptions(opts, 'noc').map((o) => o.value), ['custom:u1'])
assert.equal(m.templateTitle({ report_type: 'custom', custom_report_id: 'gone' }, catalog), 'Organisation template (deleted)')
assert.equal(m.templateTitle({ report_type: 'availability', report_title: 'From API' }, catalog), 'From API')
assert.equal(m.templateTitle({ report_type: 'availability' }, catalog), 'Availability & SLA')

/* Validation --------------------------------------------------------------- */
const tpl = opts.find((o) => o.value === 'availability')
const form = { ...m.defaultForm(), report_type: 'availability', name: 'Daily SLA', notify_channels: ['c1'] }
assert.deepEqual(m.validateSchedule(form, { template: tpl }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, name: '  ' }, { template: tpl }).errors.map((e) => e.field), ['name'])
assert.deepEqual(m.validateSchedule({ ...form, report_type: '' }, { template: null }).errors.map((e) => e.field), ['template'])
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'custom', custom_report_id: 'gone' }, { template: null }).errors.map((e) => e.field), ['template'])
assert.deepEqual(m.validateSchedule({ ...form, format: 'csv' }, { template: tpl }).errors.map((e) => e.field), ['format'])
const legacy = opts.find((o) => o.value === 'executive_summary')
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary', format: 'csv' }, { template: legacy }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary', format: 'html' }, { template: legacy }).errors.map((e) => e.field), ['format'])
const unavailable = opts.find((o) => o.value === 'apm_performance')
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'apm_performance' }, { template: unavailable }).errors.map((e) => e.field), ['template'])
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'monthly', day_of_month: 30 }, { template: tpl }).errors.map((e) => e.field), ['monthday'])
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'monthly', day_of_month: 30 }, { template: tpl, originalDayOfMonth: 30 }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, minute: 5 }, { template: tpl }).errors.map((e) => e.field), ['time'])
assert.deepEqual(m.validateSchedule({ ...form, minute: 5 }, { template: tpl, originalTime: { hour: 8, minute: 5 } }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, recipients: ['bad'] }, { template: tpl }).errors.map((e) => e.field), ['recipients'])
assert.deepEqual(m.validateSchedule({ ...form, recipients: [...many, 'z@example.com'] }, { template: tpl }).errors.map((e) => e.field), ['recipients'])
const w = m.validateSchedule({ ...form, notify_channels: [], filters: { netflow: { exporter: '10.0.0.1' } } }, { template: tpl }).warnings
assert.deepEqual(w.map((x) => x.field), ['delivery', 'scope'])

/* Request body ------------------------------------------------------------- */
const body = m.buildBody({
  ...form, frequency: 'weekly', day_of_week: 5, day_of_month: 9, description: '  ',
  filters: { netflow_withheld: true, device_ids: [], application_id: null, netflow: { exporter: '10.0.0.1' } },
})
assert.equal(body.day_of_week, 5)
assert.equal(body.day_of_month, null)
assert.equal(body.description, null)
assert.equal(body.custom_report_id, null)
assert.deepEqual(body.filters, { netflow: { exporter: '10.0.0.1' } })
// A withheld NetFlow scope is never overwritten: the body carries no filters.
const kept = m.buildBody(form, { keepStoredFilters: true })
assert.equal('filters' in kept, false)
assert.equal(m.isNetflowWithheld({ filters: { netflow_withheld: true } }), true)
assert.equal(m.isNetflowWithheld({ netflow_withheld: true, filters: {} }), true)
assert.equal(m.isNetflowWithheld({ filters: {} }), false)
const fromApi = m.scheduleToForm({
  id: 's1', name: 'X', enabled: true, report_type: 'custom', custom_report_id: 'u1', period: 'weird', format: 'pdf',
  frequency: 'monthly', hour: 6, minute: 30, day_of_week: null, day_of_month: 15, notify_channels: ['c1'],
})
assert.equal(fromApi.period, 'last_24h')
assert.equal(fromApi.day_of_month, 15)
assert.deepEqual(fromApi.recipients, [])
assert.equal(m.buildBody(fromApi).custom_report_id, 'u1')
assert.deepEqual(m.scopeOf({ device_ids: ['a', 'b'], application_id: 'app' }), {
  netflow: null, deviceIds: ['a', 'b'], applicationId: 'app', any: true,
})

/* List helpers -------------------------------------------------------------- */
const list = [
  { id: '1', name: 'Daily SLA', enabled: true, last_status: 'failed', created_by: 'me', report_type: 'availability', period: 'yesterday', format: 'pdf', next_run_at: '2026-10-08T05:00:00Z', notify_channels: [] },
  { id: '2', name: 'Weekly NOC', enabled: false, last_status: 'success', created_by: 'other', report_type: 'availability', period: 'previous_week', format: 'excel', recipients: ['noc@example.com'], notify_channels: [] },
  { id: '3', name: 'Monthly exec', enabled: true, last_status: null, created_by: 'me', report_type: 'executive_summary', period: 'previous_month', format: 'pdf', next_run_at: '2026-10-07T12:00:00Z', notify_channels: [] },
]
const titleOf = (s) => m.templateTitle(s, catalog)
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'failing', titleOf }).map((s) => s.id), ['1'])
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'paused', titleOf }).map((s) => s.id), ['2'])
assert.deepEqual(m.filterSchedules(list, { q: 'noc@example', status: 'all', titleOf }).map((s) => s.id), ['2'])
assert.deepEqual(m.filterSchedules(list, { q: 'executive', status: 'all', titleOf }).map((s) => s.id), ['3'])
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'all', mineOnly: true, userId: 'me', titleOf }).map((s) => s.id), ['1', '3'])
assert.equal(m.soonestRun(list, t).schedule.id, '3')
assert.equal(m.runHealth('partial'), 'partial')
assert.equal(m.runHealth(null), 'never')
assert.equal(m.isFailing({ last_status: 'error' }), true)

/* Access ------------------------------------------------------------------- */
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], scope_tags: ['site-a'] }), true)
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], role_scope_tags: ['site-a'] }), true)
assert.equal(m.isVisibilityScoped({ permissions: ['system.admin'], scope_tags: ['site-a'] }), false)
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], scope_tags: [] }), false)
assert.equal(m.isVisibilityScoped(null), false)

console.log('report schedules ok')
