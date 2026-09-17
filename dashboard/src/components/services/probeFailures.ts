import type { ServiceMetricPoint } from '@/types'

export type ProbeFailure = {
  start: number
  end: number
  samples: number
  reason?: string | null
  failureStage?: string
  carriedIn: boolean
  endKind: 'recovered' | 'window' | 'gap' | 'continued'
}

/** Match SLA coverage: a result lasts until the next sample, at most two intervals. */
export function buildProbeFailures(points: ServiceMetricPoint[], start: number, end: number, intervalSeconds: number): ProbeFailure[] {
  const grace = Math.max(1, intervalSeconds) * 2000
  const byTime = new Map<number, ServiceMetricPoint>()
  for (const point of points) {
    const t = Date.parse(point.timestamp)
    if (!Number.isFinite(t) || t > end) continue
    // The SLA query uses min(is_up) for duplicate timestamps: failure takes priority.
    if (!byTime.has(t) || point.is_up === false) byTime.set(t, point)
  }
  const sorted = [...byTime].sort(([a], [b]) => a - b)
  const failures: ProbeFailure[] = []
  for (let i = 0; i < sorted.length; i++) {
    const [t, point] = sorted[i]
    if (point.is_up !== false) continue
    const next = sorted[i + 1]
    const lo = Math.max(start, t)
    const hi = Math.min(end, next?.[0] ?? end, t + grace)
    if (hi <= lo) continue
    const endKind: ProbeFailure['endKind'] = next?.[0] === hi && next[1].is_up === true
      ? 'recovered' : next?.[0] === hi && next[1].is_up === false ? 'continued' : hi < end ? 'gap' : 'window'
    const stage = point.network_diagnostics?.failure_stage
    const previous = failures[failures.length - 1]
    if (previous && previous.end === lo && previous.reason === point.error_message && previous.failureStage === stage) {
      previous.end = hi
      previous.samples += t >= start ? 1 : 0
      previous.endKind = endKind
    } else {
      failures.push({ start: lo, end: hi, samples: t >= start ? 1 : 0, reason: point.error_message,
        failureStage: stage, carriedIn: t < start, endKind })
    }
  }
  return failures.reverse()
}

/** The metrics endpoint caps raw results at 5,000. Split full pages rather than lose later failures. */
export async function loadProbeHistory(
  read: (start: number, end: number) => Promise<ServiceMetricPoint[]>,
  start: number,
  end: number,
): Promise<ServiceMetricPoint[]> {
  const points = await read(start, end)
  if (points.length < 5000) return points
  // API bounds have second precision. Inclusive boundaries are deduplicated above.
  const middle = Math.floor((start + end) / 2000) * 1000
  if (middle <= start || middle >= end) throw new Error('Probe history exceeds the per-second result limit')
  const left = await loadProbeHistory(read, start, middle)
  const right = await loadProbeHistory(read, middle, end)
  return [...left, ...right]
}

export function probeHistoryEmptyMessage(downtimeSeconds: number, hasCoverage: boolean): string {
  if (downtimeSeconds > 0.001) return 'Failed checks affected uptime for this day, but their individual probe details are unavailable.'
  if (!hasCoverage) return 'No probe data available for this day.'
  return 'No failed checks found in the available probe history.'
}
