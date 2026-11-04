import { regionalDate } from '@/lib/regionalTime'
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

/** Absolute event time in the configured company timezone. */
export function absoluteTime(iso?: string | null): string {
  if (!iso) return '—'
  return regionalDate(new Date(iso), 'datetime')
}

export function pct(n: number, d: number): string {
  return d ? `${Math.round((n / d) * 100)}%` : 'N/A'
}

export const CATEGORY_LABELS: Record<string, string> = {
  blocked: 'Prerequisite missing', not_enrolled: 'Not enrolled', credentials: 'Credentials unavailable',
  authentication: 'Authentication rejected',
  unreachable: 'Unreachable', timeout: 'Timed out', negotiation: 'SSH negotiation failed', privilege: 'Insufficient privileges',
  driver: 'Driver / detection', unsupported_type: 'Unsupported capture type', incomplete: 'Incomplete output',
  format: 'Unexpected output format', interrupted: 'Session interrupted', archive: 'Archive / storage', cancelled: 'Cancelled',
  busy: 'Another capture running', key: 'SSH key problem', unknown: 'Unknown',
}

/** Capture deadline bounds (server: ncm_collection.CAPTURE_TIMEOUT_MIN/MAX). */
export const CAPTURE_TIMEOUT_MIN = 60
export const CAPTURE_TIMEOUT_MAX = 3600

/** Validation message for an optional capture deadline field ('' = inherit). */
export function captureTimeoutError(value: string): string | undefined {
  if (!value.trim()) return undefined
  const n = Number(value)
  return Number.isInteger(n) && n >= CAPTURE_TIMEOUT_MIN && n <= CAPTURE_TIMEOUT_MAX
    ? undefined : `Enter ${CAPTURE_TIMEOUT_MIN}–${CAPTURE_TIMEOUT_MAX} seconds, or leave blank.`
}

export const DEADLINE_SOURCE: Record<string, string> = {
  device: 'this device', profile: 'connection profile', driver: 'driver default', system: 'system default',
}

/** Live counters of a running capture (ncm_jobs.progress). */
export type CaptureProgress = {
  phase?: string; command?: string | null; platform?: string | null; config_type?: string
  bytes?: number; lines?: number; pages?: number; elapsed_seconds?: number
  capture_elapsed_seconds?: number | null; deadline_seconds?: number | null
}

export function formatBytes(n?: number | null): string {
  if (n == null) return '—'
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MiB`
  if (n >= 1024) return `${Math.round(n / 1024)} KiB`
  return `${n} B`
}

/** One-line summary of a capture's progress, e.g. "Capturing show full-configuration: 1.2 MiB, 40,000 lines, 700 pages, 95 s of 900 s". */
export function progressSummary(p?: CaptureProgress | null): string | null {
  if (!p || !p.phase) return null
  const phase = p.phase === 'connecting' ? 'Connecting' : p.phase === 'validating' ? 'Validating' : p.phase === 'finished' ? 'Finished' : 'Capturing'
  if (p.phase === 'connecting') return `${phase}${p.elapsed_seconds != null ? ` (${Math.round(p.elapsed_seconds)} s)` : ''}`
  const parts = [formatBytes(p.bytes), `${(p.lines || 0).toLocaleString()} lines`]
  if (p.pages) parts.push(`${p.pages.toLocaleString()} pages`)
  const elapsed = p.capture_elapsed_seconds ?? p.elapsed_seconds
  if (elapsed != null) parts.push(p.deadline_seconds ? `${Math.round(elapsed)} s of ${p.deadline_seconds} s` : `${Math.round(elapsed)} s`)
  return `${phase}${p.command ? ` ${p.command}` : ''}${p.config_type ? ` (${p.config_type})` : ''}: ${parts.join(', ')}`
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
