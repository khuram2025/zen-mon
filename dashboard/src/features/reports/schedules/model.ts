/**
 * Report schedules: types and pure helpers (no React).
 *
 * Mirrors server/app/api/v1/report_schedules.py and
 * report_scheduler.compute_next_run: run times are wall-clock times in the
 * schedule's own IANA time zone (`timezone`, defaulting to the organisation
 * time zone of Settings → General → Company when the schedule is created;
 * a later organisation change does not move it), day_of_week is
 * 1=Monday … 7=Sunday, and a monthly day beyond the month's length runs on
 * its last day. Unit-tested by scripts/test-report-schedules.mjs.
 */
import { formatDate, formatDateTime, formatTime, isValidTimezone, zoneLabel, type Branding } from '@/lib/branding'
import { PERIODS, PERIOD_LABEL, isPeriodId, periodRange, zonedToUtc } from '../periods'
import type { CatalogTemplate, CustomTemplate, PeriodId, ReportCatalog, ScheduleFormat } from '../types'

/* ------------------------------------------------------------------ */
/*  Contract                                                           */
/* ------------------------------------------------------------------ */

export type ScheduleFrequency = 'daily' | 'weekly' | 'monthly'

export type ScheduleFilters = {
  netflow?: Record<string, unknown>
  device_ids?: string[]
  application_id?: string | null
  /** List API: the stored NetFlow scope is hidden from this caller. */
  netflow_withheld?: boolean
  [key: string]: unknown
}

/** A schedule as listed by GET /report-schedules. */
export type ReportSchedule = {
  id: string
  name: string
  description?: string | null
  enabled: boolean
  report_type: string
  /** Set when report_type === 'custom'. */
  custom_report_id?: string | null
  period: string
  format: string
  filters?: ScheduleFilters | null
  frequency: string
  hour: number
  minute: number
  day_of_week?: number | null
  day_of_month?: number | null
  /** IANA zone hour/minute are in; null on rows saved before migration 163 (organisation zone). */
  timezone?: string | null
  notify_channels: string[]
  recipients?: string[]
  // Bookkeeping returned by the API (read-only in the form).
  /** The zone the schedule runs in (its own, else the organisation's). */
  effective_timezone?: string | null
  organisation_timezone?: string | null
  /** "08:00 Asia/Riyadh (05:00 UTC)". */
  local_time_label?: string | null
  /** "Daily at 08:00 Asia/Riyadh (05:00 UTC)". */
  schedule_label?: string | null
  /** next_run_at in the schedule's zone (ISO with offset). */
  next_run_local?: string | null
  /** The time written in the name when it differs from the stored time ("08:00"). */
  name_time_mismatch?: string | null
  last_run_at?: string | null
  last_status?: string | null
  last_error?: string | null
  next_run_at?: string | null
  owner_id?: string | null
  owner_name?: string | null
  /** The caller owns the schedule (it runs with their permissions). */
  is_owner?: boolean
  report_title?: string | null
  can_edit?: boolean
  netflow_withheld?: boolean
  created_at?: string | null
  updated_at?: string | null
}

/** Body for POST /report-schedules and PUT /report-schedules/{id}. */
export type ScheduleBody = {
  name: string
  description: string | null
  enabled: boolean
  report_type: string
  custom_report_id: string | null
  period: PeriodId
  format: ScheduleFormat
  /** Omitted on update when the stored NetFlow scope is withheld, so it is kept. */
  filters?: ScheduleFilters
  frequency: ScheduleFrequency
  hour: number
  minute: number
  day_of_week: number | null
  day_of_month: number | null
  /** null = the organisation time zone (the server stores it explicitly). */
  timezone: string | null
  notify_channels: string[]
  recipients: string[]
}

/** One run in GET /report-schedules/{id}/runs. */
export type ScheduleRun = {
  id: string
  token: string | null
  token_withheld?: boolean
  title?: string | null
  status: string
  delivered_to: string[]
  error?: string | null
  generated_at: string | null
}

export type NotificationChannel = { id: string; name: string; type: string; enabled?: boolean }

/** The editor's working copy. */
export type ScheduleForm = {
  name: string
  description: string
  enabled: boolean
  report_type: string
  custom_report_id: string | null
  period: PeriodId
  format: ScheduleFormat
  filters: ScheduleFilters
  frequency: ScheduleFrequency
  hour: number
  minute: number
  day_of_week: number
  day_of_month: number
  /** IANA zone; '' = the organisation time zone. */
  timezone: string
  notify_channels: string[]
  recipients: string[]
}

/* ------------------------------------------------------------------ */
/*  Vocabulary                                                         */
/* ------------------------------------------------------------------ */

export const FREQUENCIES: { id: ScheduleFrequency; label: string }[] = [
  { id: 'daily', label: 'Daily' },
  { id: 'weekly', label: 'Weekly' },
  { id: 'monthly', label: 'Monthly' },
]

/** ISO weekdays, 1 = Monday … 7 = Sunday (the API's day_of_week). */
export const WEEKDAYS: { id: number; short: string; long: string }[] = [
  { id: 1, short: 'Mon', long: 'Monday' },
  { id: 2, short: 'Tue', long: 'Tuesday' },
  { id: 3, short: 'Wed', long: 'Wednesday' },
  { id: 4, short: 'Thu', long: 'Thursday' },
  { id: 5, short: 'Fri', long: 'Friday' },
  { id: 6, short: 'Sat', long: 'Saturday' },
  { id: 7, short: 'Sun', long: 'Sunday' },
]

/** Weekday chips in the organisation's week order. */
export function weekdaysFrom(weekStart: string | undefined): typeof WEEKDAYS {
  const first = weekStart === 'sunday' ? 7 : weekStart === 'saturday' ? 6 : 1
  const i = WEEKDAYS.findIndex((d) => d.id === first)
  return [...WEEKDAYS.slice(i), ...WEEKDAYS.slice(0, i)]
}

/** Highest monthly day offered: every month has it, so no month is skipped. */
export const MAX_MONTH_DAY = 28
export const MAX_RECIPIENTS = 50

export const FORMAT_META: Record<ScheduleFormat, { label: string; hint: string }> = {
  pdf: { label: 'PDF', hint: 'Attached as a PDF document' },
  excel: { label: 'Excel', hint: 'Attached as an Excel workbook' },
  html: { label: 'HTML', hint: 'Attached as a self-contained web page' },
  csv: { label: 'CSV', hint: 'Attached as CSV tables' },
  none: { label: 'Summary only', hint: 'Email summary with a link, no attachment' },
}

/** Attachment formats a report engine can deliver. */
export function formatsForEngine(engine: 'legacy' | 'sections'): ScheduleFormat[] {
  return engine === 'legacy' ? ['pdf', 'excel', 'csv', 'none'] : ['pdf', 'excel', 'html', 'none']
}

export function formatLabel(format: string | null | undefined): string {
  return (format && FORMAT_META[format as ScheduleFormat]?.label) || (format ? format.toUpperCase() : '—')
}

export function periodLabel(period: string | null | undefined): string {
  return (period && PERIOD_LABEL[period]) || (period ? period.replace(/_/g, ' ') : '—')
}

/** Report period suggested for a frequency: each run covers the last complete interval. */
export function suggestedPeriod(frequency: string): PeriodId {
  if (frequency === 'weekly') return 'previous_week'
  if (frequency === 'monthly') return 'previous_month'
  return 'yesterday'
}

const pad = (n: number) => String(n).padStart(2, '0')

/** "08:00" or "8:00 AM". */
export function clockLabel(hour: number, minute: number, timeFormat: string = '24h'): string {
  const h = ((hour % 24) + 24) % 24
  if (timeFormat === '12h') return `${h % 12 || 12}:${pad(minute)} ${h < 12 ? 'AM' : 'PM'}`
  return `${pad(h)}:${pad(minute)}`
}

/** Times of day in 15-minute steps, 00:00 … 23:45. */
export const TIME_STEPS: { hour: number; minute: number; value: string }[] = Array.from({ length: 96 }, (_, i) => {
  const hour = Math.floor(i / 4)
  const minute = (i % 4) * 15
  return { hour, minute, value: `${pad(hour)}:${pad(minute)}` }
})

export function ordinal(n: number): string {
  const s = ['th', 'st', 'nd', 'rd']
  const v = n % 100
  return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`
}

type Timing = Pick<ReportSchedule, 'frequency' | 'hour' | 'minute' | 'day_of_week' | 'day_of_month'>

/** "Daily at 08:00", "Weekly on Monday at 08:00", "Monthly on day 1 at 08:00". */
export function frequencyText(s: Timing, timeFormat: string = '24h'): string {
  const at = clockLabel(s.hour ?? 0, s.minute ?? 0, timeFormat)
  if (s.frequency === 'weekly') {
    const day = WEEKDAYS.find((d) => d.id === (s.day_of_week || 1))?.long ?? 'Monday'
    return `Weekly on ${day} at ${at}`
  }
  if (s.frequency === 'monthly') return `Monthly on day ${s.day_of_month || 1} at ${at}`
  return `Daily at ${at}`
}

/* ------------------------------------------------------------------ */
/*  Time                                                               */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/*  Schedule time zone                                                 */
/* ------------------------------------------------------------------ */

/** The zone a schedule runs in: its own when valid, else the organisation's, else UTC. */
export function scheduleZone(s: { timezone?: string | null } | null | undefined, orgTz: string): string {
  const own = (s?.timezone ?? '').trim()
  if (own && isValidTimezone(own)) return own
  return orgTz && isValidTimezone(orgTz) ? orgTz : 'UTC'
}

/** "Asia/Riyadh" → "Asia/Riyadh"; underscores read as spaces ("America/New York"). */
export function zoneName(tz: string): string {
  return tz.replace(/_/g, ' ')
}

/** "05:00 UTC". */
export function utcClock(at: Date): string {
  return `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())} UTC`
}

/**
 * The schedule's time with the UTC equivalent of its next run, like the
 * server's local_time_label: "08:00 Asia/Riyadh (05:00 UTC)",
 * "01:00 Asia/Riyadh (22:00 UTC, previous day)", "08:00 UTC".
 */
export function localTimeLabel(s: Timing, tz: string, now: Date = new Date(), timeFormat = '24h'): string {
  const local = clockLabel(s.hour ?? 0, s.minute ?? 0, timeFormat)
  if (!isValidTimezone(tz) || tz === 'UTC' || tz === 'Etc/UTC') return `${local} UTC`
  const at = nextRuns(s, tz, 1, now)[0]
  if (!at) return `${local} ${zoneName(tz)}`
  const wall = wallDate(at, tz)
  const utcDay = Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate())
  const localDay = Date.UTC(wall.y, wall.m - 1, wall.d)
  const day = utcDay < localDay ? ', previous day' : utcDay > localDay ? ', next day' : ''
  return `${local} ${zoneName(tz)} (${utcClock(at)}${day})`
}

/** "Mon 12-Oct-2026 08:00 GMT+3 · 05:00 UTC" for a run instant in the schedule's zone. */
export function runWithUtc(value: Date, b: Fmt): string {
  const local = `${runLabel(value, b)} ${zoneLabel(b.timezone, value)}`
  return zoneLabel(b.timezone, value) === 'UTC' ? local : `${local} · ${utcClock(value)}`
}

// No lookbehind (Safari < 16.4 rejects it at parse time and the whole page
// fails): the character before the time is captured instead.
const TIME_IN_NAME = /(^|[^\d:.])([01]?\d|2[0-3])[:h]([0-5]\d)(?:\s*([AaPp])\.?[Mm]\b\.?)?(?![\d:])/g

/**
 * The time of day written in a schedule name ("NOC Daily 08:00", "08h30",
 * "7:30 PM") when it differs from the stored local time, else null — the
 * server's name_time_mismatch. A title is not evidence of when it runs.
 */
export function nameTimeMismatch(name: string | null | undefined, hour: number, minute: number): string | null {
  if (!name) return null
  for (const m of name.matchAll(TIME_IN_NAME)) {
    let h = Number(m[2])
    const min = Number(m[3])
    const ampm = (m[4] || '').toLowerCase()
    if (ampm) {
      if (h < 1 || h > 12) continue
      h = (h % 12) + (ampm === 'p' ? 12 : 0)
    }
    if (h !== hour || min !== minute) return m[0].slice(m[1].length).trim()
  }
  return null
}

/** Naive API timestamps are UTC. */
export function toInstant(value: string | number | Date | null | undefined): Date | null {
  if (value == null || value === '') return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') return new Date(value)
  const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value)
  const d = new Date(naive ? `${value.replace(' ', 'T')}Z` : value)
  return Number.isNaN(d.getTime()) ? null : d
}

function wallDate(at: Date, tz: string): { y: number; m: number; d: number } {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: isValidTimezone(tz) ? tz : 'UTC', year: 'numeric', month: 'numeric', day: 'numeric',
  })
  const p: Record<string, number> = {}
  for (const part of fmt.formatToParts(at)) if (part.type !== 'literal') p[part.type] = Number(part.value)
  return { y: p.year, m: p.month, d: p.day }
}

function daysInMonth(y: number, m: number): number {
  return new Date(Date.UTC(y, m, 0)).getUTCDate()
}

/** ISO weekday (1 = Monday … 7 = Sunday) of a calendar date. */
function isoWeekday(y: number, m: number, d: number): number {
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay() || 7
}

/**
 * The next `count` run instants after `now`, computed like the server's
 * compute_next_run: wall-clock hour:minute in `tz`; weekly on day_of_week;
 * monthly on day_of_month, clamped to the month's last day.
 */
export function nextRuns(s: Timing, tz: string, count = 3, now: Date = new Date()): Date[] {
  const out: Date[] = []
  const hour = Math.min(23, Math.max(0, Number(s.hour) || 0))
  const minute = Math.min(59, Math.max(0, Number(s.minute) || 0))
  const dow = Math.min(7, Math.max(1, Number(s.day_of_week) || 1))
  const dom = Math.min(31, Math.max(1, Number(s.day_of_month) || 1))
  const start = wallDate(now, tz)
  // Two years of days covers any monthly schedule several times over.
  for (let i = 0; i < 800 && out.length < count; i++) {
    const t = new Date(Date.UTC(start.y, start.m - 1, start.d + i))
    const y = t.getUTCFullYear()
    const m = t.getUTCMonth() + 1
    const d = t.getUTCDate()
    if (s.frequency === 'weekly' && isoWeekday(y, m, d) !== dow) continue
    if (s.frequency === 'monthly' && d !== Math.min(dom, daysInMonth(y, m))) continue
    const at = zonedToUtc(y, m, d, tz, hour, minute)
    if (at.getTime() > now.getTime()) out.push(at)
  }
  return out
}

/**
 * Relative wording that is right for both directions:
 * future "in 3 h", "in 25 min", "in 2 d"; past "3 h ago"; "just now".
 */
export function relativeTo(value: string | number | Date | null | undefined, now: number = Date.now()): string {
  const t = toInstant(value)
  if (!t) return '—'
  const diff = t.getTime() - now
  const abs = Math.abs(diff)
  const MIN = 60_000
  const HOUR = 60 * MIN
  const DAY = 24 * HOUR
  let span: string
  if (abs < MIN) return diff > 0 ? 'in under a minute' : 'just now'
  if (abs < HOUR) span = `${Math.floor(abs / MIN)} min`
  else if (abs < 3 * HOUR) {
    const h = Math.floor(abs / HOUR)
    const m = Math.floor((abs % HOUR) / MIN)
    span = m ? `${h} h ${m} min` : `${h} h`
  } else if (abs < 2 * DAY) span = `${Math.floor(abs / HOUR)} h`
  else if (abs < 60 * DAY) span = `${Math.floor(abs / DAY)} d`
  else span = `${Math.floor(abs / (30 * DAY))} mo`
  return diff > 0 ? `in ${span}` : `${span} ago`
}

type Fmt = Pick<Branding, 'timezone' | 'date_format' | 'time_format'>

/** Weekday name of an instant in the organisation timezone ("Mon"). */
export function weekdayIn(value: Date, tz: string): string {
  return new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: isValidTimezone(tz) ? tz : 'UTC' }).format(value)
}

/** "Mon 2026-10-12 08:00" in the organisation's formats (no zone). */
export function runLabel(value: Date, b: Fmt): string {
  return `${weekdayIn(value, b.timezone)} ${formatDateTime(value, b)}`
}

/** Periods that end before the run starts and cover whole days. */
const COMPLETE_PERIODS = new Set<PeriodId>(['yesterday', 'previous_week', 'previous_month', 'previous_quarter'])

/**
 * The data window a run at `runAt` covers: "2026-09-01 – 2026-09-30 (GMT+3)"
 * for complete calendar periods, "2026-10-06 08:00 – 2026-10-07 08:00 GMT+3"
 * for rolling and to-date ones.
 */
export function coverageText(period: PeriodId, runAt: Date, b: Fmt & Pick<Branding, 'week_start'>): string {
  const { from, to } = periodRange(period, b.timezone, b.week_start, runAt)
  const zone = zoneLabel(b.timezone, to)
  if (COMPLETE_PERIODS.has(period)) {
    const last = new Date(to.getTime() - 1)
    const a = formatDate(from, b)
    const z = formatDate(last, b)
    return a === z ? `${a}, whole day (${zone})` : `${a} – ${z}, whole days (${zone})`
  }
  const sameDay = formatDate(from, b) === formatDate(to, b)
  return sameDay
    ? `${formatDate(from, b)} ${formatTime(from, b)} – ${formatTime(to, b)} ${zone}`
    : `${formatDateTime(from, b)} – ${formatDateTime(to, b)} ${zone}`
}

/** True when the zone's offset at `at` differs from its offset at `ref` (a clock change in between). */
export function crossesClockChange(tz: string, at: Date, ref: Date): boolean {
  return zoneLabel(tz, at) !== zoneLabel(tz, ref)
}

/* ------------------------------------------------------------------ */
/*  Recipients                                                         */
/* ------------------------------------------------------------------ */

const EMAIL_RE = /^[^\s@,;<>()[\]"]+@[^\s@,;<>()[\]"]+\.[^\s@,;<>()[\]"]{2,}$/

export function isEmail(value: string): boolean {
  return value.length <= 254 && EMAIL_RE.test(value)
}

/** Split pasted text ("a@x.com, b@y.com; <c@z.com>") into candidate addresses. */
export function splitAddresses(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((t) => t.trim().replace(/^<|>$/g, '').replace(/^mailto:/i, ''))
    .filter(Boolean)
}

/** Add addresses to a recipient list: valid, de-duplicated (case-insensitive), at most MAX_RECIPIENTS. */
export function addRecipients(
  current: string[],
  text: string,
  max = MAX_RECIPIENTS,
): { next: string[]; invalid: string[]; duplicates: string[]; overflow: string[] } {
  const next = [...current]
  const seen = new Set(current.map((r) => r.toLowerCase()))
  const invalid: string[] = []
  const duplicates: string[] = []
  const overflow: string[] = []
  for (const addr of splitAddresses(text)) {
    if (!isEmail(addr)) { invalid.push(addr); continue }
    const key = addr.toLowerCase()
    if (seen.has(key)) { duplicates.push(addr); continue }
    if (next.length >= max) { overflow.push(addr); continue }
    seen.add(key)
    next.push(addr)
  }
  return { next, invalid, duplicates, overflow }
}

/* ------------------------------------------------------------------ */
/*  Templates                                                          */
/* ------------------------------------------------------------------ */

export type TemplateOption = {
  /** report_type, or `custom:<uuid>` for an organisation template. */
  value: string
  report_type: string
  custom_report_id: string | null
  title: string
  description: string
  category: string
  categoryTitle: string
  icon?: string
  engine: 'legacy' | 'sections'
  filterable: string[]
  defaultPeriod?: PeriodId | null
  frequency?: ScheduleFrequency
  custom: boolean
  owner?: string | null
  available: boolean
}

export const templateValue = (reportType: string, customId?: string | null) =>
  reportType === 'custom' && customId ? `custom:${customId}` : reportType

/** Schedulable templates: available built-ins (legacy engine included) and organisation templates. */
export function templateOptions(catalog: ReportCatalog | undefined): TemplateOption[] {
  if (!catalog) return []
  const catTitle = new Map((catalog.categories ?? []).map((c) => [c.id, c.title]))
  const builtIn = (catalog.types ?? []).map((t: CatalogTemplate): TemplateOption => ({
    value: t.key,
    report_type: t.key,
    custom_report_id: null,
    title: t.title,
    description: t.description,
    category: t.category,
    categoryTitle: t.category_title || catTitle.get(t.category) || t.category || 'Other',
    icon: t.icon,
    engine: t.engine === 'legacy' ? 'legacy' : 'sections',
    filterable: t.filterable ?? [],
    defaultPeriod: t.default_period ?? null,
    frequency: t.frequency,
    custom: false,
    available: t.available !== false,
  }))
  const custom = (catalog.custom ?? []).map((c: CustomTemplate): TemplateOption => ({
    value: `custom:${c.id}`,
    report_type: 'custom',
    custom_report_id: c.id,
    title: c.name,
    description: c.description || '',
    category: c.category || '',
    categoryTitle: 'Organisation templates',
    engine: 'sections',
    filterable: ['devices', 'applications', 'netflow'],
    defaultPeriod: c.default_period,
    custom: true,
    owner: c.owner_name,
    available: true,
  }))
  return [...builtIn, ...custom]
}

/** Group options by category in the catalog's category order; organisation templates last. */
export function groupTemplateOptions(
  options: TemplateOption[],
  catalog: ReportCatalog | undefined,
): { id: string; title: string; options: TemplateOption[] }[] {
  const order = new Map((catalog?.categories ?? []).map((c, i) => [c.id, i]))
  const groups = new Map<string, { id: string; title: string; options: TemplateOption[] }>()
  for (const o of options) {
    const id = o.custom ? '__custom' : o.category || '__other'
    if (!groups.has(id)) groups.set(id, { id, title: o.custom ? 'Organisation templates' : o.categoryTitle, options: [] })
    groups.get(id)!.options.push(o)
  }
  const rank = (id: string) => (id === '__custom' ? 1e6 : id === '__other' ? 1e5 : order.get(id) ?? 1e4)
  return [...groups.values()].sort((a, b) => rank(a.id) - rank(b.id) || a.title.localeCompare(b.title))
}

export function searchTemplateOptions(options: TemplateOption[], q: string): TemplateOption[] {
  const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return options
  return options.filter((o) => {
    const hay = `${o.title} ${o.description} ${o.categoryTitle} ${o.owner ?? ''}`.toLowerCase()
    return terms.every((t) => hay.includes(t))
  })
}

/** Title of a schedule's template: API title, then catalog, then the key. */
export function templateTitle(s: Pick<ReportSchedule, 'report_type' | 'custom_report_id' | 'report_title'>, catalog?: ReportCatalog): string {
  if (s.report_title) return s.report_title
  if (s.report_type === 'custom') {
    const c = catalog?.custom?.find((x) => x.id === s.custom_report_id)
    return c ? c.name : 'Organisation template (deleted)'
  }
  return catalog?.types?.find((t) => t.key === s.report_type)?.title || s.report_type.replace(/_/g, ' ')
}

/* ------------------------------------------------------------------ */
/*  Scope                                                              */
/* ------------------------------------------------------------------ */

export function isNetflowWithheld(s: Pick<ReportSchedule, 'netflow_withheld' | 'filters'> | null | undefined): boolean {
  return !!s && (s.netflow_withheld === true || s.filters?.netflow_withheld === true)
}

export type ScopeSummary = {
  netflow: Record<string, unknown> | null
  deviceIds: string[]
  applicationId: string | null
  any: boolean
}

export function scopeOf(filters: ScheduleFilters | null | undefined): ScopeSummary {
  const f = filters ?? {}
  const netflow = f.netflow && typeof f.netflow === 'object' && Object.keys(f.netflow).length ? f.netflow : null
  const deviceIds = Array.isArray(f.device_ids) ? f.device_ids.filter((x): x is string => typeof x === 'string' && !!x) : []
  const applicationId = typeof f.application_id === 'string' && f.application_id ? f.application_id : null
  return { netflow, deviceIds, applicationId, any: !!netflow || deviceIds.length > 0 || !!applicationId }
}

/** Filters to send: no bookkeeping flags, no empty scopes. */
export function cleanFilters(filters: ScheduleFilters | null | undefined): ScheduleFilters {
  const out: ScheduleFilters = {}
  for (const [k, v] of Object.entries(filters ?? {})) {
    if (k === 'netflow_withheld') continue
    if (v == null || v === '') continue
    if (Array.isArray(v) && v.length === 0) continue
    if (typeof v === 'object' && !Array.isArray(v) && Object.keys(v as object).length === 0) continue
    out[k] = v
  }
  return out
}

/* ------------------------------------------------------------------ */
/*  Form                                                               */
/* ------------------------------------------------------------------ */

export function defaultForm(): ScheduleForm {
  return {
    name: '', description: '', enabled: true,
    report_type: '', custom_report_id: null,
    period: 'yesterday', format: 'pdf', filters: {},
    frequency: 'daily', hour: 8, minute: 0, day_of_week: 1, day_of_month: 1,
    timezone: '',
    notify_channels: [], recipients: [],
  }
}

const asFrequency = (v: string | null | undefined): ScheduleFrequency =>
  v === 'weekly' || v === 'monthly' ? v : 'daily'

export function scheduleToForm(s: ReportSchedule): ScheduleForm {
  return {
    name: s.name || '',
    description: s.description || '',
    enabled: s.enabled ?? true,
    report_type: s.report_type || '',
    custom_report_id: s.custom_report_id ?? null,
    period: isPeriodId(s.period) ? s.period : 'last_24h',
    format: (['pdf', 'excel', 'csv', 'html', 'none'].includes(s.format) ? s.format : 'pdf') as ScheduleFormat,
    filters: { ...(s.filters ?? {}) },
    frequency: asFrequency(s.frequency),
    hour: s.hour ?? 8,
    minute: s.minute ?? 0,
    day_of_week: s.day_of_week ?? 1,
    day_of_month: s.day_of_month ?? 1,
    timezone: (s.timezone ?? '').trim(),
    notify_channels: [...(s.notify_channels ?? [])],
    recipients: [...(s.recipients ?? [])],
  }
}

/** Suggested schedule name: "<template> — weekly". */
export function suggestedName(title: string, frequency: ScheduleFrequency): string {
  return title ? `${title} — ${frequency}` : ''
}

export function buildBody(form: ScheduleForm, opts: { keepStoredFilters?: boolean } = {}): ScheduleBody {
  const body: ScheduleBody = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    enabled: form.enabled,
    report_type: form.report_type,
    custom_report_id: form.report_type === 'custom' ? form.custom_report_id : null,
    period: form.period,
    format: form.format,
    frequency: form.frequency,
    hour: form.hour,
    minute: form.minute,
    day_of_week: form.frequency === 'weekly' ? form.day_of_week : null,
    day_of_month: form.frequency === 'monthly' ? form.day_of_month : null,
    timezone: form.timezone.trim() || null,
    notify_channels: [...form.notify_channels],
    recipients: [...form.recipients],
  }
  // A withheld NetFlow scope is not in the form: sending filters would erase it.
  if (!opts.keepStoredFilters) body.filters = cleanFilters(form.filters)
  return body
}

export type Issue = { field: string; message: string }

export type ValidationContext = {
  template: TemplateOption | null
  /** The schedule's stored day of month (an existing 29–31 stays allowed). */
  originalDayOfMonth?: number | null
  /** The stored time (an existing off-step minute stays allowed). */
  originalTime?: { hour: number; minute: number } | null
  /** The zone the schedule runs in (for wording; the organisation zone when unset). */
  timezone?: string
}

export function validateSchedule(form: ScheduleForm, ctx: ValidationContext): { errors: Issue[]; warnings: Issue[] } {
  const errors: Issue[] = []
  const warnings: Issue[] = []
  if (!form.report_type) errors.push({ field: 'template', message: 'Choose a report template.' })
  else if (form.report_type === 'custom' && !form.custom_report_id) errors.push({ field: 'template', message: 'Choose an organisation template.' })
  else if (!ctx.template) errors.push({ field: 'template', message: 'The selected template no longer exists. Choose another one.' })
  else if (!ctx.template.available) errors.push({ field: 'template', message: 'Your role cannot run the selected template. Choose another one.' })

  if (!isPeriodId(form.period)) errors.push({ field: 'period', message: 'Choose the data period.' })

  const offStep = form.minute % 15 !== 0
  const keepsTime = !!ctx.originalTime && ctx.originalTime.hour === form.hour && ctx.originalTime.minute === form.minute
  if (form.hour < 0 || form.hour > 23 || form.minute < 0 || form.minute > 59 || (offStep && !keepsTime)) {
    errors.push({ field: 'time', message: 'Choose a time of day in 15-minute steps.' })
  }
  if (form.timezone.trim() && !isValidTimezone(form.timezone.trim())) {
    errors.push({ field: 'timezone', message: 'Choose a valid time zone.' })
  }
  if (form.frequency === 'weekly' && !(form.day_of_week >= 1 && form.day_of_week <= 7)) {
    errors.push({ field: 'weekday', message: 'Choose the day of the week.' })
  }
  if (form.frequency === 'monthly') {
    const kept = ctx.originalDayOfMonth != null && ctx.originalDayOfMonth === form.day_of_month
    if (!(form.day_of_month >= 1 && (form.day_of_month <= MAX_MONTH_DAY || (kept && form.day_of_month <= 31)))) {
      errors.push({ field: 'monthday', message: `Choose a day of the month between 1 and ${MAX_MONTH_DAY}.` })
    }
  }

  if (ctx.template && !formatsForEngine(ctx.template.engine).includes(form.format)) {
    errors.push({ field: 'format', message: `${formatLabel(form.format)} is not available for this template.` })
  }

  const badRecipients = form.recipients.filter((r) => !isEmail(r))
  if (badRecipients.length) errors.push({ field: 'recipients', message: `Fix ${badRecipients.length} invalid email address${badRecipients.length === 1 ? '' : 'es'}.` })
  if (form.recipients.length > MAX_RECIPIENTS) errors.push({ field: 'recipients', message: `Use at most ${MAX_RECIPIENTS} email recipients.` })

  const name = form.name.trim()
  if (!name) errors.push({ field: 'name', message: 'Enter a schedule name.' })
  else if (name.length > 255) errors.push({ field: 'name', message: 'Keep the name under 255 characters.' })
  const named = nameTimeMismatch(name, form.hour, form.minute)
  if (named) {
    const zone = form.timezone.trim() || ctx.timezone || ''
    warnings.push({
      field: 'name',
      message: `The name says ${named}, but the report runs at ${clockLabel(form.hour, form.minute)}${zone ? ` ${zoneName(zone)}` : ''}. Check the time or the name.`,
    })
  }

  if (form.notify_channels.length === 0 && form.recipients.length === 0) {
    warnings.push({ field: 'delivery', message: 'Nobody receives this report; runs only appear in the run history.' })
  }
  const scope = scopeOf(form.filters)
  const filterable = ctx.template?.filterable ?? []
  if (ctx.template && scope.netflow && !filterable.includes('netflow')) {
    warnings.push({ field: 'scope', message: 'This template has no NetFlow sections; the NetFlow scope is ignored.' })
  }
  if (ctx.template && scope.deviceIds.length && !filterable.includes('devices')) {
    warnings.push({ field: 'scope', message: 'This template is not limited by devices; the device scope is ignored.' })
  }
  if (ctx.template && scope.applicationId && !filterable.includes('applications')) {
    warnings.push({ field: 'scope', message: 'This template is not limited by application; the application scope is ignored.' })
  }
  if (form.period === 'today' && form.hour < 6) {
    warnings.push({ field: 'period', message: '“Today” at this hour covers only a few hours of data. “Yesterday” covers the whole day.' })
  }
  return { errors, warnings }
}

/* ------------------------------------------------------------------ */
/*  List                                                               */
/* ------------------------------------------------------------------ */

export type RunHealth = 'success' | 'partial' | 'failed' | 'never' | 'other'

export function runHealth(status: string | null | undefined): RunHealth {
  const s = (status || '').toLowerCase()
  if (!s) return 'never'
  if (s === 'success' || s === 'ok' || s === 'delivered') return 'success'
  if (s === 'partial') return 'partial'
  if (s === 'failed' || s === 'failure' || s === 'error') return 'failed'
  return 'other'
}

export function isFailing(s: Pick<ReportSchedule, 'last_status'>): boolean {
  return runHealth(s.last_status) === 'failed'
}

export type StatusFilter = 'all' | 'active' | 'paused' | 'failing'

/** The caller owns the schedule: the server's flag, else the owner id. */
export function isMine(s: Pick<ReportSchedule, 'is_owner' | 'owner_id'>, userId?: string): boolean {
  if (typeof s.is_owner === 'boolean') return s.is_owner
  return !!userId && !!s.owner_id && s.owner_id === userId
}

export function filterSchedules(
  list: ReportSchedule[],
  opts: { q: string; status: StatusFilter; mineOnly?: boolean; userId?: string; titleOf: (s: ReportSchedule) => string },
): ReportSchedule[] {
  const terms = opts.q.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return list.filter((s) => {
    if (opts.status === 'active' && !s.enabled) return false
    if (opts.status === 'paused' && s.enabled) return false
    if (opts.status === 'failing' && !isFailing(s)) return false
    if (opts.mineOnly && !isMine(s, opts.userId)) return false
    if (!terms.length) return true
    const hay = [
      s.name, s.description, opts.titleOf(s), s.owner_name, periodLabel(s.period), formatLabel(s.format),
      ...(s.recipients ?? []),
    ].filter(Boolean).join(' ').toLowerCase()
    return terms.every((t) => hay.includes(t))
  })
}

/** The soonest upcoming run among enabled schedules. */
export function soonestRun(list: ReportSchedule[], now: number): { schedule: ReportSchedule; at: Date } | null {
  let best: { schedule: ReportSchedule; at: Date } | null = null
  for (const s of list) {
    if (!s.enabled) continue
    const at = toInstant(s.next_run_at)
    if (!at || at.getTime() < now - 5 * 60_000) continue
    if (!best || at < best.at) best = { schedule: s, at }
  }
  return best
}

/** Rolling and calendar period groups for pickers. */
export const PERIOD_GROUPS: { title: string; periods: { id: PeriodId; label: string }[] }[] = [
  { title: 'Rolling window', periods: PERIODS.filter((p) => p.kind === 'rolling').map(({ id, label }) => ({ id, label })) },
  { title: 'Calendar period', periods: PERIODS.filter((p) => p.kind === 'calendar').map(({ id, label }) => ({ id, label })) },
]

/** Visibility-scoped accounts cannot manage schedules (the API answers 403). */
export function isVisibilityScoped(user: { permissions?: string[]; scope_tags?: string[]; role_scope_tags?: string[] } | null | undefined): boolean {
  if (!user) return false
  if (user.permissions?.includes('system.admin')) return false
  return (user.scope_tags?.length ?? 0) > 0 || (user.role_scope_tags?.length ?? 0) > 0
}

export const SCOPED_EXPLANATION =
  'Your account is limited to a visibility scope. Scheduled reports render data for the whole organisation and are emailed onward, so scoped accounts can view schedules but cannot create or change them.'
