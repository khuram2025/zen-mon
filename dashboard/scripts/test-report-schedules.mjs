// Report schedule helpers (features/reports/schedules/model.ts): frequency wording,
// next-run calculation in the schedule's own timezone (organisation fallback),
// local + UTC labels, name/time mismatch, period defaults, relative
// times in both directions, recipients, validation and the request body;
// delivery options, runs, links and report settings (docs/reports-delivery.md).
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
assert.deepEqual(m.formatsForEngine('sections'), ['pdf', 'excel', 'html', 'csv', 'none'])
assert.equal(m.frequencyText({ frequency: 'weekly', hour: 8, minute: 0, day_of_week: null }), 'Weekly at 08:00, day not chosen')

/* Period suggestions (M11) -------------------------------------------------- */
// Weekly on the first day of the organisation week: the previous week.
assert.deepEqual(m.suggestedPeriods('weekly', { dayOfWeek: 7, weekStart: 'sunday' }), ['previous_week'])
assert.deepEqual(m.suggestedPeriods('weekly', { dayOfWeek: 1, weekStart: 'monday' }), ['previous_week'])
// Any other day: "Previous week" would skip the days just completed.
assert.deepEqual(m.suggestedPeriods('weekly', { dayOfWeek: 7, weekStart: 'monday' }), ['last_7d'])
assert.deepEqual(m.suggestedPeriods('weekly', { dayOfWeek: 3, weekStart: 'sunday' }), ['last_7d'])
// The day after the Sun–Thu work week (Friday) also gets the work week.
assert.deepEqual(m.suggestedPeriods('weekly', { dayOfWeek: 5, weekStart: 'sunday' }), ['last_7d', 'previous_work_week'])
assert.equal(m.dayAfterWorkWeek('sunday'), 5)
assert.equal(m.dayAfterWorkWeek('monday'), 6)
assert.equal(m.dayAfterWorkWeek('saturday'), 4)
assert.equal(m.suggestedPeriod('weekly', { dayOfWeek: 0, weekStart: 'sunday' }), 'previous_week') // no day yet
assert.deepEqual(m.suggestedPeriods('monthly', { dayOfWeek: 3 }), ['previous_month'])

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

/* Schedule time zone (NOTIFY 07) ------------------------------------------- */
// The schedule's own zone wins; the organisation zone is only the fallback.
assert.equal(m.scheduleZone({ timezone: 'Asia/Riyadh' }, 'UTC'), RIYADH)
assert.equal(m.scheduleZone({ timezone: ' Europe/Berlin ' }, RIYADH), 'Europe/Berlin')
assert.equal(m.scheduleZone({ timezone: null }, RIYADH), RIYADH)
assert.equal(m.scheduleZone({ timezone: 'Not/AZone' }, RIYADH), RIYADH)
assert.equal(m.scheduleZone({}, 'Not/AZone'), 'UTC')
const riyadh8 = { frequency: 'daily', hour: 8, minute: 0, timezone: RIYADH }
for (const org of ['UTC', 'Europe/London', 'Asia/Tokyo']) {
  // An organisation zone change does not move a schedule that has its own zone.
  assert.deepEqual(iso(m.nextRuns(riyadh8, m.scheduleZone(riyadh8, org), 1, now)), ['2026-10-08T05:00:00.000Z'])
}
// Weekly Sunday 08:00 Riyadh: Sundays only, 05:00 UTC.
const sunday = m.nextRuns({ frequency: 'weekly', hour: 8, minute: 0, day_of_week: 7 }, RIYADH, 3, now)
assert.deepEqual(iso(sunday), ['2026-10-11T05:00:00.000Z', '2026-10-18T05:00:00.000Z', '2026-10-25T05:00:00.000Z'])
assert.ok(sunday.every((d) => m.weekdayIn(d, RIYADH) === 'Sun'))
// DST: Europe/Berlin leaves summer time on 2026-10-25; 08:00 local moves from 06:00Z to 07:00Z.
const BERLIN = 'Europe/Berlin'
const berlinNow = new Date('2026-10-23T12:00:00Z')
assert.deepEqual(iso(m.nextRuns({ frequency: 'daily', hour: 8, minute: 0 }, BERLIN, 3, berlinNow)),
  ['2026-10-24T06:00:00.000Z', '2026-10-25T07:00:00.000Z', '2026-10-26T07:00:00.000Z'])
assert.deepEqual(iso(m.nextRuns({ frequency: 'weekly', hour: 8, minute: 0, day_of_week: 7 }, BERLIN, 2, new Date('2027-03-22T00:00:00Z'))),
  ['2027-03-28T06:00:00.000Z', '2027-04-04T06:00:00.000Z'])
// Labels: local time with the UTC clock of the next run, as the server prints them.
assert.equal(m.localTimeLabel(riyadh8, RIYADH, now), '08:00 Asia/Riyadh (05:00 UTC)')
assert.equal(m.localTimeLabel({ frequency: 'daily', hour: 1, minute: 0 }, RIYADH, now), '01:00 Asia/Riyadh (22:00 UTC, previous day)')
assert.equal(m.localTimeLabel({ frequency: 'daily', hour: 8, minute: 0 }, 'UTC', now), '08:00 UTC')
assert.equal(m.localTimeLabel({ frequency: 'daily', hour: 8, minute: 0 }, BERLIN, berlinNow), '08:00 Europe/Berlin (06:00 UTC)')
assert.equal(m.localTimeLabel({ frequency: 'daily', hour: 8, minute: 0 }, BERLIN, new Date('2026-10-24T12:00:00Z')), '08:00 Europe/Berlin (07:00 UTC)')
assert.equal(m.localTimeLabel({ frequency: 'daily', hour: 20, minute: 0 }, 'America/New_York', nyNow, '12h'), '8:00 PM America/New York (00:00 UTC, next day)')
const rb = { timezone: RIYADH, date_format: 'DD-MMM-YYYY', time_format: '24h' }
assert.equal(m.runWithUtc(new Date('2026-10-11T05:00:00Z'), rb), 'Sun 11-Oct-2026 08:00 GMT+3 · 05:00 UTC')
assert.equal(m.runWithUtc(new Date('2026-10-11T05:00:00Z'), { ...rb, timezone: 'UTC' }), 'Sun 11-Oct-2026 05:00 UTC')
assert.equal(m.utcClock(new Date('2026-10-11T05:00:00Z')), '05:00 UTC')
// A time in the name that differs from the stored time (the field case: "08:00" saved as 05:00).
assert.equal(m.nameTimeMismatch('NOC Daily Briefing 08:00 Riyadh', 5, 0), '08:00')
assert.equal(m.nameTimeMismatch('NOC Daily Briefing 08:00 Riyadh', 8, 0), null)
assert.equal(m.nameTimeMismatch('Ops digest 7:30 PM', 19, 30), null)
assert.equal(m.nameTimeMismatch('Ops digest 7:30 PM', 7, 30), '7:30 PM')
assert.equal(m.nameTimeMismatch('Brief 08h30', 8, 0), '08h30')
for (const n of ['Weekly SLA', 'Release 1.24 notes', 'Week of 18.10.2026', 'Ratio 3:2:1', '', null]) {
  assert.equal(m.nameTimeMismatch(n, 8, 0), null, String(n))
}

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
// Friday 08:00 with a Sunday week start: the Sun–Thu just ended.
assert.equal(m.coverageText('previous_work_week', new Date('2026-10-09T05:00:00Z'), { ...branding, week_start: 'sunday' }),
  '04-Oct-2026 – 08-Oct-2026, whole days (GMT+3)')
// The field case: weekly Sunday run, Sunday week start, previous week = the week just ended.
assert.equal(m.coverageText('previous_week', new Date('2026-10-25T05:00:00Z'), { ...branding, week_start: 'sunday' }),
  '18-Oct-2026 – 24-Oct-2026, whole days (GMT+3)')
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
// Aligned with report_scheduler.is_email.
assert.equal(m.isEmail("o'brien+ops@mail.example.com"), true)
assert.equal(m.isEmail('a..b@example.com'), false)
assert.equal(m.isEmail('.a@example.com'), false)
assert.equal(m.isEmail('a.@example.com'), false)
assert.equal(m.isEmail('a@-example.com'), false)
assert.equal(m.isEmail('a@example..com'), false)
assert.equal(m.isEmail('Ops <ops@example.com>'), false)
assert.equal(m.isEmail(`${'a'.repeat(65)}@example.com`), false)

/* Subject template ---------------------------------------------------------- */
assert.equal(m.renderSubject('{title} — {period} ({date})', { title: 'Availability', period: 'Previous week', date: '25-Oct-2026' }),
  'Availability — Previous week (25-Oct-2026)')
assert.deepEqual(m.unknownPlaceholders('{title} {Title} {site} {date}'), ['{Title}', '{site}'])
assert.deepEqual(m.unknownPlaceholders('{ title }'), [])

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
const legacyOpt = () => opts.find((o) => o.value === 'executive_summary')
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
assert.deepEqual(m.validateSchedule({ ...form, format: 'csv' }, { template: tpl }).errors, []) // CSV for templates
const legacy = opts.find((o) => o.value === 'executive_summary')
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary', format: 'csv' }, { template: legacy }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary', format: 'html' }, { template: legacy }).errors.map((e) => e.field), ['format'])
const unavailable = opts.find((o) => o.value === 'apm_performance')
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'apm_performance' }, { template: unavailable }).errors.map((e) => e.field), ['template'])
// Monthly days 1–31; a day beyond the month's length runs on its last day.
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'monthly', day_of_month: 31 }, { template: tpl }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'monthly', day_of_month: 32 }, { template: tpl }).errors.map((e) => e.field), ['monthday'])
assert.equal(m.MAX_MONTH_DAY, 31)
// Weekly needs a day.
assert.equal(m.defaultForm().day_of_week, 0)
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'weekly', day_of_week: 0 }, { template: tpl }).errors.map((e) => e.field), ['weekday'])
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'weekly', day_of_week: 4 }, { template: tpl }).errors, [])
assert.equal(m.buildBody({ ...form, frequency: 'weekly', day_of_week: 0 }).day_of_week, null)
// Weekly "Previous week" on a day other than the week start is a warning.
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'weekly', day_of_week: 7, period: 'previous_week' }, { template: tpl, weekStart: 'monday' })
  .warnings.map((x) => x.field), ['period'])
assert.deepEqual(m.validateSchedule({ ...form, frequency: 'weekly', day_of_week: 7, period: 'previous_week' }, { template: tpl, weekStart: 'sunday' })
  .warnings.map((x) => x.field), [])
// Name: trimmed, not empty.
assert.deepEqual(m.validateSchedule({ ...form, name: '\t ' }, { template: tpl }).errors.map((e) => e.field), ['name'])
// Classic types: kept by an existing schedule, refused for new ones.
assert.equal(legacyOpt().deprecated, true)
assert.equal(legacyOpt().replacement, 'executive_overview')
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary' }, { template: legacyOpt(), isNew: true }).errors.map((e) => e.field), ['template'])
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary' }, { template: legacyOpt(), original: { report_type: 'executive_summary', format: 'pdf' } }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, report_type: 'executive_summary' }, { template: legacyOpt(), original: { report_type: 'availability', format: 'pdf' } }).errors.map((e) => e.field), ['template'])
// Summary only needs a report link; an existing one is kept with a warning.
assert.deepEqual(m.validateSchedule({ ...form, format: 'none' }, { template: tpl, shareLinksAvailable: false }).errors.map((e) => e.field), ['format'])
assert.deepEqual(m.validateSchedule({ ...form, format: 'none' }, { template: tpl, shareLinksAvailable: true }).errors, [])
const keptNone = m.validateSchedule({ ...form, format: 'none' }, { template: tpl, shareLinksAvailable: false, original: { report_type: 'availability', format: 'none' } })
assert.deepEqual(keptNone.errors, [])
assert.ok(keptNone.warnings.some((x) => x.field === 'format'))
// Email options.
assert.deepEqual(m.validateSchedule({ ...form, reply_to: 'not an email' }, { template: tpl }).errors.map((e) => e.field), ['reply_to'])
assert.deepEqual(m.validateSchedule({ ...form, reply_to: 'noc@example.com' }, { template: tpl }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, recipients: ['a@example.com'], group_send: true, cc: ['bad'] }, { template: tpl }).errors.map((e) => e.field), ['cc'])
assert.deepEqual(m.validateSchedule({ ...form, group_send: false, cc: ['bad'] }, { template: tpl }).errors, []) // cc unused
assert.deepEqual(m.validateSchedule({ ...form, recipients: ['a@example.com'], group_send: true, cc: [...many, 'z@example.com'] }, { template: tpl }).errors.map((e) => e.field), ['cc'])
assert.deepEqual(m.validateSchedule({ ...form, subject_template: 'x'.repeat(256) }, { template: tpl }).errors.map((e) => e.field), ['subject'])
assert.deepEqual(m.validateSchedule({ ...form, subject_template: '{title} for {site}' }, { template: tpl }).warnings.map((x) => x.field), ['subject'])
assert.deepEqual(m.validateSchedule({ ...form, minute: 5 }, { template: tpl }).errors.map((e) => e.field), ['time'])
assert.deepEqual(m.validateSchedule({ ...form, minute: 5 }, { template: tpl, originalTime: { hour: 8, minute: 5 } }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, recipients: ['bad'] }, { template: tpl }).errors.map((e) => e.field), ['recipients'])
assert.deepEqual(m.validateSchedule({ ...form, recipients: [...many, 'z@example.com'] }, { template: tpl }).errors.map((e) => e.field), ['recipients'])
const w = m.validateSchedule({ ...form, notify_channels: [], filters: { netflow: { exporter: '10.0.0.1' } } }, { template: tpl }).warnings
assert.deepEqual(w.map((x) => x.field), ['delivery', 'scope'])

// Name/time disagreement is a warning (with the zone); an unknown zone is an error.
const mis = m.validateSchedule({ ...form, name: 'NOC Daily 08:00', hour: 5 }, { template: tpl, timezone: RIYADH })
assert.deepEqual(mis.errors, [])
assert.deepEqual(mis.warnings.find((x) => x.field === 'name')?.message,
  'The name says 08:00, but the report runs at 05:00 Asia/Riyadh. Check the time or the name.')
assert.equal(m.validateSchedule({ ...form, name: 'NOC Daily 08:00' }, { template: tpl }).warnings.some((x) => x.field === 'name'), false)
assert.deepEqual(m.validateSchedule({ ...form, timezone: 'Mars/Olympus' }, { template: tpl }).errors.map((e) => e.field), ['timezone'])

/* Request body ------------------------------------------------------------- */
const mail = m.buildBody({ ...form, subject_template: '  {title}  ', reply_to: ' ', group_send: false, cc: ['x@example.com'], skip_when_empty: true })
assert.equal(mail.subject_template, '{title}')
assert.equal(mail.reply_to, null)
assert.deepEqual(mail.cc, [])                    // cc only with group_send
assert.equal(mail.skip_when_empty, true)
assert.deepEqual(m.buildBody({ ...form, group_send: true, cc: ['x@example.com'] }).cc, ['x@example.com'])
const hidden = m.buildBody(form, { keepStoredRecipients: true })
assert.equal('recipients' in hidden, false)      // withheld recipients are kept
assert.equal('cc' in hidden, false)
const emailForm = m.scheduleToForm({ id: 'e', name: 'E', enabled: true, report_type: 'availability', period: 'previous_work_week', format: 'csv',
  frequency: 'weekly', hour: 8, minute: 0, day_of_week: 5, notify_channels: [], subject_template: 'S', reply_to: 'r@example.com', group_send: true, cc: ['c@example.com'], skip_when_empty: true })
assert.equal(emailForm.period, 'previous_work_week')
assert.equal(emailForm.format, 'csv')
assert.deepEqual([emailForm.subject_template, emailForm.reply_to, emailForm.group_send, emailForm.cc, emailForm.skip_when_empty],
  ['S', 'r@example.com', true, ['c@example.com'], true])
const body = m.buildBody({
  ...form, frequency: 'weekly', day_of_week: 5, day_of_month: 9, description: '  ',
  filters: { netflow_withheld: true, device_ids: [], application_id: null, netflow: { exporter: '10.0.0.1' } },
})
assert.equal(body.day_of_week, 5)
assert.equal(body.day_of_month, null)
assert.equal(body.description, null)
assert.equal(body.custom_report_id, null)
assert.deepEqual(body.filters, { netflow: { exporter: '10.0.0.1' } })
// '' = the organisation zone (the server stores it explicitly); a chosen zone is sent as is.
assert.equal(body.timezone, null)
assert.equal(m.buildBody({ ...form, timezone: ' Europe/Berlin ' }).timezone, 'Europe/Berlin')
assert.equal(m.defaultForm().timezone, '')
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
assert.equal(fromApi.timezone, '')                                   // pre-migration row: organisation zone
assert.equal(m.scheduleToForm({ ...fromApi, timezone: RIYADH, notify_channels: [] }).timezone, RIYADH)
assert.equal(m.buildBody(m.scheduleToForm({ id: 'x', name: 'Dup', enabled: true, report_type: 'availability', period: 'yesterday',
  format: 'pdf', frequency: 'daily', hour: 8, minute: 0, timezone: BERLIN, notify_channels: [] })).timezone, BERLIN) // duplicate keeps it
assert.equal(fromApi.day_of_month, 15)
assert.deepEqual(fromApi.recipients, [])
assert.equal(m.buildBody(fromApi).custom_report_id, 'u1')
assert.deepEqual(m.scopeOf({ device_ids: ['a', 'b'], application_id: 'app' }), {
  netflow: null, deviceIds: ['a', 'b'], groupIds: [], locations: [], deviceTypes: [], applicationId: 'app', any: true,
})
// Group / location / type scope alone is a scope too.
const grouped = m.scopeOf({ group_ids: ['g1'], locations: ['Jeddah', ''], device_types: [] })
assert.deepEqual([grouped.groupIds, grouped.locations, grouped.any], [['g1'], ['Jeddah'], true])
assert.deepEqual(m.cleanFilters({ group_ids: [], locations: ['Jeddah'], device_ids: [] }), { locations: ['Jeddah'] })

/* List helpers -------------------------------------------------------------- */
const list = [
  { id: '1', name: 'Daily SLA', enabled: true, last_status: 'failed', owner_id: 'me', is_owner: true, report_type: 'availability', period: 'yesterday', format: 'pdf', next_run_at: '2026-10-08T05:00:00Z', notify_channels: [] },
  { id: '2', name: 'Weekly NOC', enabled: false, last_status: 'success', owner_id: 'other', is_owner: false, report_type: 'availability', period: 'previous_week', format: 'excel', recipients: ['noc@example.com'], notify_channels: [] },
  { id: '3', name: 'Monthly exec', enabled: true, last_status: null, owner_id: 'me', is_owner: true, report_type: 'executive_summary', period: 'previous_month', format: 'pdf', next_run_at: '2026-10-07T12:00:00Z', notify_channels: [] },
]
const titleOf = (s) => m.templateTitle(s, catalog)
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'failing', titleOf }).map((s) => s.id), ['1'])
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'paused', titleOf }).map((s) => s.id), ['2'])
assert.deepEqual(m.filterSchedules(list, { q: 'noc@example', status: 'all', titleOf }).map((s) => s.id), ['2'])
assert.deepEqual(m.filterSchedules(list, { q: 'executive', status: 'all', titleOf }).map((s) => s.id), ['3'])
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'all', mineOnly: true, userId: 'me', titleOf }).map((s) => s.id), ['1', '3'])
// The API returns owner_id and is_owner (never created_by); is_owner wins.
assert.deepEqual(m.filterSchedules(list, { q: '', status: 'all', mineOnly: true, titleOf }).map((s) => s.id), ['1', '3'])
assert.equal(m.isMine({ owner_id: 'me' }, 'me'), true)
assert.equal(m.isMine({ owner_id: 'me', is_owner: false }, 'me'), false)
assert.equal(m.isMine({ owner_id: null }, 'me'), false)
assert.equal(m.soonestRun(list, t).schedule.id, '3')
assert.equal(m.runHealth('partial'), 'partial')
assert.equal(m.runHealth('running'), 'running')
assert.equal(m.runHealth('skipped'), 'skipped')
const flagged = [
  { ...list[0], id: 'o', overdue: true },
  { ...list[1], id: 'r', suggested_time: { hour: 8, minute: 0 } },
  { ...list[2], id: 'n' },
]
assert.deepEqual(m.filterSchedules(flagged, { q: '', status: 'overdue', titleOf }).map((s) => s.id), ['o'])
assert.deepEqual(m.filterSchedules(flagged, { q: '', status: 'retime', titleOf }).map((s) => s.id), ['r'])
assert.equal(m.isLegacySchedule({ report_type: 'executive_summary' }), true)
assert.equal(m.isLegacySchedule({ report_type: 'availability' }, catalog), false)
assert.equal(m.isLegacySchedule({ report_type: 'availability', is_legacy: true }), true)
assert.equal(m.replacementOf({ report_type: 'device_health' }), 'availability')
assert.equal(m.replacementOf({ report_type: 'device_health', replacement: 'other' }), 'other')
assert.equal(m.catalogTitle('availability', catalog), 'Availability & SLA')
assert.equal(m.runHealth(null), 'never')
assert.equal(m.isFailing({ last_status: 'error' }), true)

/* Access ------------------------------------------------------------------- */
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], scope_tags: ['site-a'] }), true)
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], role_scope_tags: ['site-a'] }), true)
assert.equal(m.isVisibilityScoped({ permissions: ['system.admin'], scope_tags: ['site-a'] }), false)
assert.equal(m.isVisibilityScoped({ permissions: ['reports.manage'], scope_tags: [] }), false)
assert.equal(m.isVisibilityScoped(null), false)

/* Runs ---------------------------------------------------------------------- */
assert.deepEqual(m.runNotes(null), [])
assert.deepEqual(m.runNotes(['Section "Top talkers" failed', { message: 'Attachment left out (14 MB)' }]),
  ['Section "Top talkers" failed', 'Attachment left out (14 MB)'])
assert.deepEqual(m.runNotes({ failed_sections: ['top_talkers', 'cpu'], attachment_omitted: true }),
  ['Failed sections: top_talkers, cpu', 'Attachment omitted'])
assert.deepEqual(m.runNotes('Plain note'), ['Plain note'])
const chMap = new Map([['c1', { name: 'NOC Teams' }]])
assert.deepEqual(m.failedTargetLabels(['a@example.com', { channel_id: 'c1', error: 'x' }, { address: 'b@example.com' }], chMap),
  ['a@example.com', 'NOC Teams', 'b@example.com'])
assert.deepEqual(m.failedTargetLabels(null), [])
assert.equal(m.canRetryRun({ status: 'partial', failed_targets: ['a@example.com'] }), true)
assert.equal(m.canRetryRun({ status: 'partial', failed_targets: [] }), false)
assert.equal(m.canRetryRun({ status: 'running', failed_targets: ['a@example.com'] }), false)
assert.equal(m.canRetryRun({ status: 'partial', failed_targets: [], can_retry: false }), false)
assert.equal(m.canRetryRun({ status: 'failed', failed_targets: [{ kind: 'email', name: 'a@example.com' }], can_retry: true }), true)
assert.deepEqual(m.failedTargetLabels([{ kind: 'channel', name: 'NOC Teams', error: 'HTTP 500' }]), ['NOC Teams'])
assert.equal(m.linkState({ token: null, revoked: true, expires_at: '2026-10-01T00:00:00Z' }, t), 'ended')
assert.equal(m.linkState({ token: 't', expires_at: '2026-10-01T00:00:00Z' }, t), 'ended')
assert.equal(m.linkState({ token: null, token_withheld: true, expires_at: '2026-10-01T00:00:00Z' }, t), 'ended')
assert.equal(m.linkState({ token: 't', expires_at: '2026-11-01T00:00:00Z' }, t), 'active')
assert.equal(m.linkState({ token: 't', expires_at: null }, t), 'never_expires')
assert.equal(m.linkState({ token: null, expires_at: null }, t), 'none')
assert.equal(m.retryText({ next_retry_at: '2026-10-07T05:20:00Z', retry_count: 0 }, 3, rb), 'Retrying at 08:20, attempt 1 of 3')
assert.equal(m.retryText({ next_retry_at: '2026-10-07T05:20:00Z', retry_count: 1 }, null, rb), 'Retrying at 08:20, attempt 2')
assert.equal(m.retryText({ next_retry_at: null, retry_count: 1 }, 3, rb), null)
assert.equal(m.runKindLabel('retry'), 'Retry')
assert.deepEqual(m.searchRuns([{ title: 'Daily SLA', schedule_name: 'NOC', status: 'failed', period: 'yesterday' }, { title: 'Other', status: 'success' }], 'noc yesterday').map((r) => r.title), ['Daily SLA'])

/* Report settings ------------------------------------------------------------ */
const okSettings = { link_expiry_days: 0, run_retention_days: 7, artifact_retention_days: 365, max_attachment_mb: 50, retry_attempts: 5 }
assert.deepEqual(m.validateReportSettings(okSettings), {})
assert.deepEqual(Object.keys(m.validateReportSettings({ ...okSettings, link_expiry_days: 366, run_retention_days: 6, artifact_retention_days: '', max_attachment_mb: 2.5, retry_attempts: -1 })),
  ['link_expiry_days', 'run_retention_days', 'artifact_retention_days', 'max_attachment_mb', 'retry_attempts'])
assert.deepEqual(m.REPORT_SETTING_DEFAULTS, { link_expiry_days: 30, run_retention_days: 180, artifact_retention_days: 30, max_attachment_mb: 10, retry_attempts: 3 })

/* Run-now results, retry plan, CC rule ------------------------------------------ */
assert.deepEqual(m.runResultMessage({ status: 'skipped', delivered: [], errors: [] }),
  { tone: 'warning', title: 'Not sent', body: 'Not sent: the report had no data (the schedule skips empty reports).' })
assert.equal(m.runResultMessage({ status: 'skipped' }, 'Daily SLA').title, '"Daily SLA" not sent')
assert.deepEqual(m.runResultMessage({ status: 'failed', errors: ['SMTP refused'] }), { tone: 'error', title: 'Run failed', body: 'SMTP refused' })
assert.deepEqual(m.runResultMessage({ status: 'partial', delivered: ['a@example.com'], errors: ['b@example.com: refused'] }),
  { tone: 'warning', title: 'Delivered with problems', body: 'Delivered to 1 recipient. b@example.com: refused' })
assert.equal(m.runResultMessage({ status: 'success', delivered: ['a', 'b'] }).body, 'Delivered to 2 recipients.')
assert.equal(m.runResultMessage({ status: 'success', delivered: ['a'] }, 'X').title, '"X" generated')
assert.equal(m.retryPlanText(3), 'Failed deliveries are retried 3 times: after 5, 15 and 45 minutes.')
assert.equal(m.retryPlanText(1), 'Failed deliveries are retried once: after 5 minutes.')
assert.equal(m.retryPlanText(5), 'Failed deliveries are retried 5 times: after 5, 15, 45, 120 and 240 minutes.')
assert.match(m.retryPlanText(0), /not retried automatically/)
assert.deepEqual(m.validateSchedule({ ...form, recipients: [], group_send: true, cc: ['c@example.com'] }, { template: tpl }).errors.map((e) => e.field), ['cc'])
assert.deepEqual(m.validateSchedule({ ...form, recipients: ['a@example.com'], group_send: true, cc: ['c@example.com'] }, { template: tpl }).errors, [])
assert.deepEqual(m.validateSchedule({ ...form, recipients: [], group_send: true, cc: ['c@example.com'] }, { template: tpl, recipientsWithheld: true }).errors, [])

console.log('report schedules ok')
