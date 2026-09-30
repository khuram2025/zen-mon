/**
 * Formatting shared by the service detail page. Empty-value language is
 * deliberate and consistent (review UI 11):
 *   NO_DATA         — nothing was measured
 *   NOT_SET         — a configuration field was left blank
 *   NOT_APPLICABLE  — the field has no meaning for this monitor type
 *   NEVER_CHECKED   — the monitor has not produced a result yet
 */
import { uptimeBand, type UptimeBand } from '@/components/services/uptime'

export const NO_DATA = 'No data'
export const NOT_SET = 'Not set'
export const NOT_APPLICABLE = 'Not applicable'
export const NEVER_CHECKED = 'Never checked'

export function formatDur(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return '—'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${Math.floor(sec % 60)}s`
  return `${Math.floor(sec)}s`
}

/** Duration where a measured zero is meaningful ("0s"), and null is "No data". */
export function formatDurMeasured(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return NO_DATA
  if (sec <= 0) return '0s'
  return formatDur(sec)
}

export function formatMs(n: number | null | undefined, empty = '—'): string {
  if (n == null || !Number.isFinite(n)) return empty
  if (n >= 1000) return `${(n / 1000).toFixed(1)} s`
  return `${n.toFixed(n < 10 ? 1 : 0)} ms`
}

export function pct(n: number | null | undefined, digits = 2, empty = '—'): string {
  if (n == null || !Number.isFinite(n)) return empty
  return `${n.toFixed(digits)}%`
}

/**
 * Availability label (B17). "100%" is reserved for zero confirmed downtime;
 * anything else is truncated — never rounded up — so a day with 31 s down
 * reads 99.96%, not 100%. When the downtime is unknown the percentage decides.
 */
export function formatAvailability(
  p: number | null | undefined,
  downtimeSec?: number | null,
  digits = 2,
  empty = NO_DATA,
): string {
  if (p == null || !Number.isFinite(p)) return empty
  const hadDowntime = downtimeSec != null ? downtimeSec > 0.0005 : p < 100
  if (!hadDowntime) return '100%'
  const f = 10 ** digits
  const truncated = Math.floor(Math.min(p, 100) * f) / f
  if (truncated >= 100) return `>${(100 - 1 / f).toFixed(digits)}%`
  return `${truncated.toFixed(digits)}%`
}

/** Colour band that never shows the "perfect" colour for a period with downtime. */
export function availabilityBand(p: number | null | undefined, downtimeSec?: number | null): UptimeBand {
  const band = uptimeBand(p)
  if (band === 'perfect' && (downtimeSec ?? 0) > 0.0005) return 'good'
  return band
}

/**
 * The shared UptimeBars colour by percentage only. Nudge a value with
 * downtime below the "perfect" threshold so its bar colour agrees with the
 * label; titles keep the real figure.
 */
export function colourPct(p: number | null, downtimeSec: number): number | null {
  if (p == null) return null
  return downtimeSec > 0.0005 ? Math.min(p, 99.94) : p
}

export function exactTime(t: number | string | null | undefined, withSeconds = true): string {
  if (t == null) return '—'
  const d = typeof t === 'number' ? new Date(t) : new Date(t)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
  })
}

export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time'
  } catch {
    return 'local time'
  }
}

/** "UTC+03:00" for the browser's current offset. */
export function utcOffsetLabel(at: number = Date.now()): string {
  const off = -new Date(at).getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const abs = Math.abs(off)
  return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`
}

export function isUnsupported(err: unknown): boolean {
  const status = (err as any)?.response?.status
  return status === 404 || status === 405 || status === 501
}

export function httpStatus(err: unknown): number | undefined {
  return (err as any)?.response?.status
}

export function cleanAlertMessage(msg: string): string {
  return (msg || 'Alert').replace(/^\[ZenPlus\s+[A-Z]+\]\s*/, '')
}

/** A recovery notification is stored as its own resolved alert; recognise it. */
export function isRecoveryAlert(a: { metadata?: { is_recovery?: boolean } | null; message?: string }): boolean {
  if (a.metadata?.is_recovery === true) return true
  if (a.metadata?.is_recovery === false) return false
  return /passing again|— recovered$/i.test(a.message || '')
}
