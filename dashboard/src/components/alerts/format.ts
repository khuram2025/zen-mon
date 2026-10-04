/**
 * Alert Center time formatting (review A13, A27).
 *
 * - Absolute dates always name the timezone ("30 Sep 2026, 14:05 UTC+3") and
 *   include the year when it is not the appliance's current year (or always,
 *   with `year: 'always'`).
 * - Ages are measured against the appliance clock — the response
 *   `server_time` when known, else the calibrated `serverNow()` — never the
 *   browser clock, which can be hours off.
 * - A timestamp later than the appliance's "now" is reported as a future
 *   timestamp instead of being presented as fresh.
 */
import { useEffect, useRef, useState } from 'react'
import { serverNow } from '@/lib/serverClock'

/** Parse backend timestamps; naive values are UTC. */
export function parseTs(v: string | number | null | undefined): number | null {
  if (v == null || v === '') return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  let s = v
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = `${s.replace(' ', 'T')}Z`
  const t = Date.parse(s)
  return Number.isNaN(t) ? null : t
}

/** "UTC+3", "UTC−4:30", "UTC" for the viewer's offset at that instant. */
export function utcOffsetLabel(ms: number = Date.now()): string {
  const off = -new Date(ms).getTimezoneOffset()
  if (off === 0) return 'UTC'
  const sign = off > 0 ? '+' : '−'
  const abs = Math.abs(off)
  const h = Math.floor(abs / 60)
  const m = abs % 60
  return `UTC${sign}${h}${m ? `:${String(m).padStart(2, '0')}` : ''}`
}

/** Browser timezone name + offset, e.g. "Asia/Riyadh (UTC+3)". */
export function timezoneName(): string {
  let name = 'Local time'
  try {
    name = Intl.DateTimeFormat().resolvedOptions().timeZone || name
  } catch { /* older browsers */ }
  return `${name} (${utcOffsetLabel()})`
}

/**
 * `30 Sep, 14:05 UTC+3` (current year) / `30 Sep 2025, 14:05 UTC+3`.
 * The "current year" is the appliance's.
 */
export function fmtAlertDate(
  v: string | number | null | undefined,
  opts: { seconds?: boolean; year?: 'auto' | 'always'; tz?: boolean } = {},
): string {
  const ms = parseTs(v)
  if (ms == null) return '—'
  const d = new Date(ms)
  const showYear = opts.year === 'always' || d.getFullYear() !== new Date(serverNow()).getFullYear()
  const date = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(showYear ? { year: 'numeric' as const } : {}) })
  const time = d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', ...(opts.seconds ? { second: '2-digit' as const } : {}), hour12: false,
  })
  return `${date}, ${time}${opts.tz === false ? '' : ` ${utcOffsetLabel(ms)}`}`
}

/** `14:05:09` in the viewer timezone. */
export function fmtClock(ms: number | null | undefined, seconds = true): string {
  if (ms == null) return '—'
  return new Date(ms).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' as const } : {}), hour12: false,
  })
}

/** Compact duration: `45s`, `12m`, `3h 12m`, `4d 6h`. null → '—'. */
export function fmtDur(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return h ? `${d}d ${h}h` : `${d}d`
  if (h > 0) return m ? `${h}h ${m}m` : `${h}h`
  return `${m}m`
}

/** Exact duration with seconds precision: `90` → `1m 30s`, `3600` → `1h`. */
export function fmtDurExact(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s === 0) return '0s'
  const parts: string[] = []
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  if (d) parts.push(`${d}d`)
  if (h) parts.push(`${h}h`)
  if (m) parts.push(`${m}m`)
  if (r) parts.push(`${r}s`)
  return parts.join(' ')
}

/** Minutes (response targets, escalation delays) → `15 min`, `2h`. */
export function fmtMinutes(min: number | null | undefined): string {
  if (min == null || !Number.isFinite(min)) return '—'
  if (min < 60) return `${min} min`
  return fmtDurExact(min * 60)
}

export type Age = { seconds: number | null; future: boolean }

/** Seconds since `iso` against the appliance `nowMs`. */
export function ageOf(iso: string | null | undefined, nowMs: number): Age {
  const t = parseTs(iso)
  if (t == null) return { seconds: null, future: false }
  const diff = (nowMs - t) / 1000
  // Tolerate a minute of disagreement between producers before flagging.
  if (diff < -60) return { seconds: -diff, future: true }
  return { seconds: Math.max(0, diff), future: false }
}

/** "3h 12m" / "Future timestamp" / "—". */
export function fmtAge(iso: string | null | undefined, nowMs: number): string {
  const a = ageOf(iso, nowMs)
  if (a.seconds == null) return '—'
  if (a.future) return `${fmtDur(a.seconds)} in the future`
  if (a.seconds < 10) return 'just now'
  return fmtDur(a.seconds)
}

export function fmtAgo(iso: string | null | undefined, nowMs: number): string {
  const a = ageOf(iso, nowMs)
  if (a.seconds == null) return '—'
  if (a.future) return `${fmtDur(a.seconds)} in the future`
  if (a.seconds < 10) return 'just now'
  return `${fmtDur(a.seconds)} ago`
}

/** Seconds between two timestamps (null when either is missing/invalid). */
export function spanSeconds(from: string | null | undefined, to: string | null | undefined): number | null {
  const a = parseTs(from)
  const b = parseTs(to)
  if (a == null || b == null) return null
  return Math.max(0, (b - a) / 1000)
}

/**
 * Appliance "now" anchored on the last response's `server_time`, advancing
 * with the browser's monotonic elapsed time and re-rendering every `tickMs`.
 * Falls back to the calibrated `serverNow()` until a server_time is known.
 */
export function useApplianceNow(serverTime: string | null | undefined, tickMs = 15_000): number {
  const anchor = useRef<{ server: number; local: number } | null>(null)
  const parsed = parseTs(serverTime ?? null)
  if (parsed != null && (anchor.current == null || anchor.current.server !== parsed)) {
    anchor.current = { server: parsed, local: Date.now() }
  }
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), tickMs)
    return () => window.clearInterval(id)
  }, [tickMs])
  if (anchor.current) return anchor.current.server + (Date.now() - anchor.current.local)
  return serverNow()
}
