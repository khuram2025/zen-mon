/**
 * NCCM view model: protection states, semantic colours and shared helpers.
 *
 * Colour rules (management review): red = failed/blocked/unprotected risk,
 * amber = stale/unverified/unresolved readiness, green ONLY for fresh validated
 * artifacts, blue = active/queued work, grey = deliberately excluded/disabled.
 * Every colour is paired with text (and an icon where shown).
 */
import { apiErrorMessage } from '@/lib/utils'

export type Tone = 'danger' | 'warning' | 'success' | 'info' | 'neutral'

export const STATES: Record<string, { label: string; tone: Tone; description: string }> = {
  failed: { label: 'Failed', tone: 'danger', description: 'The last backup attempt failed.' },
  blocked: { label: 'Blocked', tone: 'danger', description: 'A required prerequisite is missing, so backups cannot run.' },
  stale: { label: 'Stale', tone: 'warning', description: 'The last validated capture is older than the freshness limit.' },
  unverified: { label: 'Unverified', tone: 'warning', description: 'Only manual/unvalidated versions exist.' },
  pending: { label: 'Never captured', tone: 'warning', description: 'Enrolled, but no capture yet.' },
  unconfigured: { label: 'Not enrolled', tone: 'warning', description: 'In backup scope but not enrolled.' },
  fresh: { label: 'Protected', tone: 'success', description: 'Fresh, format-validated capture of every required type.' },
  disabled: { label: 'Disabled', tone: 'neutral', description: 'Collection deliberately disabled.' },
  excluded: { label: 'Excluded', tone: 'neutral', description: 'Outside the backup coverage scope.' },
}

export const RISK_ORDER = ['failed', 'blocked', 'stale', 'unverified', 'pending', 'unconfigured', 'fresh', 'disabled', 'excluded']

export const TONE_TEXT: Record<Tone, string> = {
  danger: 'text-danger', warning: 'text-warning', success: 'text-success', info: 'text-info', neutral: 'text-muted',
}
export const TONE_BG: Record<Tone, string> = {
  danger: 'bg-danger', warning: 'bg-warning', success: 'bg-success', info: 'bg-info', neutral: 'bg-muted/50',
}
export const TONE_BADGE: Record<Tone, 'danger' | 'warning' | 'success' | 'info' | 'outline'> = {
  danger: 'danger', warning: 'warning', success: 'success', info: 'info', neutral: 'outline',
}

export function stateOf(d: any) {
  const key = d?.backup_state || 'pending'
  return { key, ...(STATES[key] || { label: 'Unknown', tone: 'warning' as Tone, description: 'State unknown.' }) }
}

export const WEEKDAYS: [string, number][] = [['Sun', 0], ['Mon', 1], ['Tue', 2], ['Wed', 3], ['Thu', 4], ['Fri', 5], ['Sat', 6]]
export const WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

/** Schedule text, used when the API has not supplied `schedule_summary`. */
export function scheduleSummary(d: any): string {
  if (d?.schedule_summary) return d.schedule_summary
  if (!d?.schedule_enabled) return 'Not scheduled'
  if (d.schedule_type === 'daily') return `Daily at ${d.schedule_time || '—'} ${d.schedule_timezone || 'UTC'}`
  if (d.schedule_type === 'weekly') {
    const days = (d.schedule_days || []).slice().sort().map((n: number) => WEEKDAY_NAMES[n].slice(0, 3)).join(', ')
    return `Weekly ${days || '(no days)'} at ${d.schedule_time || '—'} ${d.schedule_timezone || 'UTC'}`
  }
  return `Every ${d.schedule_interval_hours || 24} h`
}

/** Help text shared by every schedule/retention form (B09). */
export const HELP = {
  scheduler: 'The scheduler checks every minute and queues a durable job when a device is due. Jobs retry transient failures up to 3 times; setup failures stop immediately.',
  retention: 'Keeps this many changed versions plus the latest snapshot and the last validated copy. Pinned, baseline and restore-tested versions are never pruned, so the stored count can exceed this number.',
  freshness: 'A device counts as protected only while its newest validated capture of every required type is younger than this.',
  timezone: 'Daily and weekly times are wall-clock times in this IANA timezone (daylight saving is handled).',
}

export const TIMEZONES: string[] = (() => {
  try {
    const list = (Intl as any).supportedValuesOf?.('timeZone') as string[] | undefined
    if (list?.length) return ['UTC', ...list.filter((z) => z !== 'UTC')]
  } catch {
    /* older browsers */
  }
  return ['UTC', 'Asia/Riyadh', 'Asia/Dubai', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Asia/Singapore']
})()

/** API error text, including structured `{code, message}` details. */
export function ncmError(e: any): string {
  const d = e?.response?.data?.detail
  if (d && typeof d === 'object' && !Array.isArray(d) && d.message) return d.message
  return apiErrorMessage(e)
}

export function errorDetail(e: any): any {
  const d = e?.response?.data?.detail
  return d && typeof d === 'object' && !Array.isArray(d) ? d : null
}

const fmtUtc = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const fmtLocal = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/** "2026-09-30 10:04:38 UTC" — appliance timestamps are UTC. */
export function absoluteUtc(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const p = Object.fromEntries(fmtUtc.formatToParts(d).map((x) => [x.type, x.value]))
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} UTC`
}

export function absoluteLocal(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : fmtLocal.format(d)
}

export function pct(n: number, d: number): string {
  return d ? `${Math.round((n / d) * 100)}%` : 'N/A'
}

export const CATEGORY_LABELS: Record<string, string> = {
  blocked: 'Prerequisite missing', not_enrolled: 'Not enrolled', credentials: 'Credentials unavailable',
  host_key_untrusted: 'SSH host key not trusted', host_key_changed: 'SSH host key changed', authentication: 'Authentication rejected',
  unreachable: 'Unreachable', timeout: 'Timed out', negotiation: 'SSH negotiation failed', privilege: 'Insufficient privileges',
  driver: 'Driver / detection', unsupported_type: 'Unsupported capture type', incomplete: 'Incomplete output',
  format: 'Unexpected output format', interrupted: 'Session interrupted', archive: 'Archive / storage', cancelled: 'Cancelled',
  busy: 'Another capture running', unknown: 'Unknown',
}

export function categoryLabel(c?: string | null) {
  return c ? CATEGORY_LABELS[c] || c.replace(/_/g, ' ') : ''
}

export function download(filename: string, content: string, type = 'text/plain') {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function toCsv(rows: (string | number | null | undefined)[][]): string {
  return rows.map((r) => r.map((v) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }).join(',')).join('\n')
}
