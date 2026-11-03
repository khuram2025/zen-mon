/** Shared display settings. Layout loads these before mounting application pages.
 * Explicit report/schedule zones remain supported; API instants stay unchanged.
 */
import { DEFAULT_BRANDING, formatDate, formatDateTime, formatTime, isValidTimezone, zoneLabel, zoneParts, type Branding } from './branding'
import { zonedToUtc } from '../features/reports/periods'
import { fromZonedInput, toZonedInput } from '../features/reports/viewer/range'

let current: Branding = DEFAULT_BRANDING
export function setRegionalTime(value: Partial<Branding>) {
  current = { ...DEFAULT_BRANDING, ...value, timezone: isValidTimezone(value.timezone || '') ? value.timezone! : 'UTC' }
}
export function regionalSettings() { return current }
export function regionalTimezone() { return current.timezone }
export function regionalZoneLabel(at: number = Date.now()) { return zoneLabel(current.timezone, at) }
export function regionalParts(at: number | Date) { return zoneParts(new Date(at), current.timezone) }
/** Midnight in the configured zone, with calendar-day arithmetic across DST. */
export function regionalDay(at: number | Date, offset = 0): Date {
  const p = regionalParts(at)
  const day = new Date(Date.UTC(p.year, p.month - 1, p.day + offset))
  return zonedToUtc(day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), current.timezone)
}
/** Inclusive calendar-day bounds for date-only audit and exception controls. */
export function regionalDateBound(value: string, end = false): string {
  const midday = regionalInputMs(value + 'T12:00')
  if (!Number.isFinite(midday)) return ''
  return new Date(+regionalDay(midday, end ? 1 : 0) - (end ? 1000 : 0)).toISOString()
}
export function regionalInput(at: number | Date) { return toZonedInput(new Date(at), current.timezone) }
export function regionalInputMs(value: string): number {
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value)) return Date.parse(value)
  const dt = fromZonedInput(value, current.timezone)
  // Reject invalid dates and non-existent spring-forward wall times.
  return dt && toZonedInput(dt, current.timezone) === value.slice(0,16) ? dt.getTime() : NaN
}

export function regionalDate(value: Date, mode: 'date' | 'time' | 'datetime', locales?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions): string {
  if (!Number.isFinite(value.getTime())) return '—'
  if (!options || Object.keys(options).length === 0) {
    if (mode === 'date') return formatDate(value, current)
    if (mode === 'time') return formatTime(value, current, true)
    return formatDateTime(value, current, { seconds: true, zone: true })
  }
  const opts = { timeZone: current.timezone, ...options }
  if (opts.hour && opts.hour12 === undefined && opts.hourCycle === undefined) opts.hour12 = current.time_format === '12h'
  if (mode === 'date') return value.toLocaleDateString(locales, opts)
  if (mode === 'time') return value.toLocaleTimeString(locales, opts)
  return value.toLocaleString(locales, opts)
}
