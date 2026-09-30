/**
 * Appliance clock calibration (Services review B01).
 *
 * Relative ranges ("last hour"), freshness ("checked 2m ago"), countdowns and
 * maintenance comparisons must use the appliance's notion of now, not the
 * browser's: a browser several hours ahead would otherwise call fresh results
 * overdue and query an empty "last hour".
 *
 * Every API response carries `X-Server-Time` (epoch ms, set by the API) and a
 * standard `Date` header (1 s resolution). Each response yields one skew
 * sample, corrected for half the round trip; the median of recent samples is
 * the working offset, so a single slow response cannot move it.
 *
 * Explicit custom timestamps are never adjusted — only "now" is calibrated.
 */
import { useSyncExternalStore } from 'react'

/** Beyond this the UI states the disagreement instead of silently correcting. */
export const SKEW_WARN_MS = 2 * 60_000

const MAX_SAMPLES = 15
const samples: number[] = []
let offsetMs = 0
let calibrated = false
let lastSampleAt = 0
const listeners = new Set<() => void>()

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

/**
 * Record one sample. `serverMs` is the appliance time the response was
 * produced; `sentMs`/`receivedMs` are browser times around the request.
 */
export function recordServerTime(serverMs: number, sentMs: number, receivedMs: number, resolutionMs = 0) {
  if (!Number.isFinite(serverMs) || !Number.isFinite(sentMs) || !Number.isFinite(receivedMs)) return
  const rtt = Math.max(0, receivedMs - sentMs)
  // Ignore very slow responses: their midpoint is too uncertain.
  if (rtt > 10_000) return
  // A `Date` header truncates to the second; centre it.
  const sample = serverMs + resolutionMs / 2 + rtt / 2 - receivedMs
  samples.push(sample)
  if (samples.length > MAX_SAMPLES) samples.shift()
  const next = Math.round(median(samples))
  lastSampleAt = receivedMs
  const changed = !calibrated || Math.abs(next - offsetMs) >= 1_000
  offsetMs = next
  calibrated = true
  if (changed) listeners.forEach((l) => l())
}

/** Read `X-Server-Time` / `Date` from response headers. */
export function recordFromHeaders(headers: Record<string, unknown> | undefined, sentMs: number, receivedMs: number) {
  if (!headers) return
  const get = (k: string) => {
    const v = (headers as any)[k] ?? (typeof (headers as any).get === 'function' ? (headers as any).get(k) : undefined)
    return v == null ? null : String(v)
  }
  const precise = get('x-server-time')
  if (precise && /^\d{12,14}$/.test(precise)) {
    recordServerTime(Number(precise), sentMs, receivedMs)
    return
  }
  const date = get('date')
  if (date) {
    const ms = Date.parse(date)
    if (Number.isFinite(ms)) recordServerTime(ms, sentMs, receivedMs, 1_000)
  }
}

/** Current appliance time in epoch ms (browser time until calibrated). */
export function serverNow(): number {
  return Date.now() + offsetMs
}

/** Positive: the appliance is ahead of this browser. */
export function clockOffsetMs(): number {
  return offsetMs
}

export function isClockCalibrated(): boolean {
  return calibrated
}

export function lastCalibrationAt(): number {
  return lastSampleAt
}

export function subscribeClock(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** React: re-renders when the calibrated offset changes by ≥ 1 s. */
export function useClockOffset(): { offsetMs: number; calibrated: boolean; skewed: boolean } {
  const off = useSyncExternalStore(subscribeClock, () => offsetMs, () => 0)
  const cal = useSyncExternalStore(subscribeClock, () => calibrated, () => false)
  return { offsetMs: off, calibrated: cal, skewed: cal && Math.abs(off) > SKEW_WARN_MS }
}

/** Human description of the disagreement, e.g. "6h 12m ahead of". */
export function describeSkew(ms: number): string {
  const abs = Math.abs(ms)
  const h = Math.floor(abs / 3_600_000)
  const m = Math.round((abs % 3_600_000) / 60_000)
  const size = h ? `${h}h ${m}m` : `${Math.max(1, m)} min`
  // offset = server - browser: positive means the browser is behind.
  return `${size} ${ms > 0 ? 'behind' : 'ahead of'}`
}
