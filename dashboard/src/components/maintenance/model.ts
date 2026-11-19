/* Maintenance windows (docs/maintenance-windows.md): the pure model behind the
 * maintenance dialog on the device and server pages. A window is one-off
 * (start + end) or recurring (weekdays + local start time + duration in a time
 * zone, optionally until a date). The server decides when a window is in force
 * and returns the current or next occurrence; this file only builds requests,
 * validates them against the server's limits and words what is shown. */

export const MAX_DURATION_MINUTES = 7 * 24 * 60

export const WEEKDAYS: { n: number; short: string }[] = [
  { n: 1, short: 'Mon' }, { n: 2, short: 'Tue' }, { n: 3, short: 'Wed' }, { n: 4, short: 'Thu' },
  { n: 5, short: 'Fri' }, { n: 6, short: 'Sat' }, { n: 7, short: 'Sun' },
]

export const DAY_PRESETS: { key: string; label: string; days: number[] }[] = [
  { key: 'every', label: 'Every day', days: [1, 2, 3, 4, 5, 6, 7] },
  { key: 'weekdays', label: 'Weekdays', days: [1, 2, 3, 4, 5] },
  { key: 'weekends', label: 'Weekends', days: [6, 7] },
]

export type MaintenanceAsset = { kind: 'device' | 'server'; id: string; name: string }

export type RecurrenceDraft = {
  days: number[]
  startTime: string // HH:MM, local to the time zone
  durationMinutes: number
  timezone: string // blank = the organisation's
  until: string // YYYY-MM-DD or blank
}

export type MaintenanceWindow = {
  id: string
  scope_type: string
  scope_label: string
  starts_at: string
  ends_at: string
  reason?: string | null
  active: boolean
  recurring?: boolean
  schedule?: string
  next_starts_at?: string | null
  next_ends_at?: string | null
}

export function defaultRecurrence(timezone: string): RecurrenceDraft {
  return { days: [1, 2, 3, 4, 5, 6, 7], startTime: '02:00', durationMinutes: 60, timezone, until: '' }
}

export function toggleDay(days: number[], n: number): number[] {
  const set = new Set(days)
  if (set.has(n)) set.delete(n)
  else set.add(n)
  return [...set].sort((a, b) => a - b)
}

export function sameDays(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((d, i) => d === b[i])
}

/** "45 min", "2 h", "1 h 30 min" (the server's wording). */
export function durationLabel(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

/** The server's schedule sentence: "Every Mon, Wed at 02:00 for 2 h (Europe/Berlin) until 2026-12-31". */
export function describeRecurrence(d: RecurrenceDraft, orgTimezone: string): string {
  const days = [...new Set(d.days)].sort((a, b) => a - b)
  const which = days.length === 7 ? 'day' : days.map((n) => WEEKDAYS[n - 1]?.short ?? '?').join(', ')
  const tz = d.timezone.trim() || orgTimezone
  const base = `Every ${which} at ${d.startTime} for ${durationLabel(d.durationMinutes)} (${tz})`
  return d.until ? `${base} until ${d.until}` : base
}

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

/** What is wrong with the draft, in the words the dialog shows, or null.
 *  The limits are the server's (1 minute to 7 days, ISO weekdays). */
export function recurrenceError(d: RecurrenceDraft, today: string): string | null {
  if (!d.days.length) return 'Choose at least one day.'
  if (d.days.some((n) => !Number.isInteger(n) || n < 1 || n > 7)) return 'Days must be Monday to Sunday.'
  if (!TIME_RE.test(d.startTime)) return 'Enter the start time as HH:MM.'
  if (!Number.isInteger(d.durationMinutes) || d.durationMinutes < 1) return 'The window must last at least 1 minute.'
  if (d.durationMinutes > MAX_DURATION_MINUTES) return 'A window can last at most 7 days.'
  if (d.until) {
    if (!DATE_RE.test(d.until)) return 'Enter the end date as YYYY-MM-DD.'
    if (d.until < today) return 'The end date is in the past.'
  }
  return null
}

export function scopeFields(asset: MaintenanceAsset): Record<string, string> {
  return asset.kind === 'server'
    ? { scope_type: 'server', scope_server_id: asset.id }
    : { scope_type: 'device', scope_device_id: asset.id }
}

/** POST /device-maintenance body for a recurring window. A blank zone is left
 *  out so the server uses the organisation's. */
export function recurringPayload(asset: MaintenanceAsset, d: RecurrenceDraft, reason: string) {
  const recurrence: Record<string, unknown> = {
    days: [...new Set(d.days)].sort((a, b) => a - b),
    start_time: d.startTime,
    duration_minutes: d.durationMinutes,
  }
  if (d.timezone.trim()) recurrence.timezone = d.timezone.trim()
  if (d.until) recurrence.until = d.until
  return { ...scopeFields(asset), recurrence, reason: reason.trim() || null }
}

/** POST /device-maintenance body for a one-off window (unchanged). */
export function oneOffPayload(asset: MaintenanceAsset, start: Date, end: Date, reason: string) {
  return { ...scopeFields(asset), starts_at: start.toISOString(), ends_at: end.toISOString(), reason: reason.trim() || null }
}

/** The occurrence a listed window shows: the one in force, else the next one. */
export function windowTiming(w: MaintenanceWindow): { state: 'Active' | 'Next' | 'Upcoming'; from: string; to: string } {
  const from = w.next_starts_at || w.starts_at
  const to = w.next_ends_at || w.ends_at
  if (w.active) return { state: 'Active', from, to }
  return { state: w.recurring ? 'Next' : 'Upcoming', from, to }
}

/** A window can be removed from this page only when it targets this asset
 *  alone; group, tag and linked-device windows are shown "via" their scope. */
export function ownWindow(w: MaintenanceWindow, asset: MaintenanceAsset): boolean {
  return w.scope_type === asset.kind
}

export function viaLabel(w: MaintenanceWindow, asset: MaintenanceAsset): string {
  if (ownWindow(w, asset)) return ''
  if (asset.kind === 'server' && !w.scope_type.startsWith('server')) return `via linked device (${w.scope_label})`
  return `via ${w.scope_label}`
}
