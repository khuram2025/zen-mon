/**
 * Appliance clock calibration.
 *
 * Relative ages ("5m ago") must be measured against the appliance clock, not the
 * viewer's: a laptop that is hours fast otherwise shows fresh jobs as old. Every
 * API response carries an HTTP `Date` header from the appliance; the offset is
 * estimated from the request midpoint (1 s resolution) and smoothed with a
 * median of recent samples. Offsets under 2 s are ignored as noise.
 */

type Listener = () => void

let offsetMs = 0
let samples: number[] = []
const listeners = new Set<Listener>()

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)]
}

export function recordServerDate(header: string | undefined | null, sentAt: number, receivedAt: number) {
  if (!header) return
  const server = Date.parse(header)
  if (Number.isNaN(server) || receivedAt - sentAt > 10_000) return // slow responses give poor estimates
  samples = [...samples.slice(-6), server + 500 - (sentAt + receivedAt) / 2]
  const next = median(samples)
  const applied = Math.abs(next) < 2000 ? 0 : next
  if (Math.abs(applied - offsetMs) >= 1000) {
    offsetMs = applied
    listeners.forEach((l) => l())
  }
}

/** Current time on the appliance clock (ms since epoch). */
export function serverNow(): number {
  return Date.now() + offsetMs
}

/** Appliance minus browser clock, in ms (0 when within noise). */
export function clockOffsetMs(): number {
  return offsetMs
}

export function subscribeClock(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
