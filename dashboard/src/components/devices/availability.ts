export type DeviceAvailabilityPoint = {
  timestamp: string
  is_up: boolean | null
  /** Percentage of successful checks, preserving partial failures in rollups. */
  uptime_pct?: number | null
  sample_count?: number | null
}

export function formatDeviceAvailability(pct: number | null | undefined): string {
  return typeof pct === 'number' && Number.isFinite(pct) ? `${pct.toFixed(2)}%` : '—'
}

export function deviceAvailabilityBuckets(points: DeviceAvailabilityPoint[], from: number, to: number) {
  const valid = points.filter(p => {
    const t = Date.parse(p.timestamp)
    return Number.isFinite(t) && t >= from && t <= to
  }).sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  const gaps = valid.slice(1).map((p, i) => Date.parse(p.timestamp) - Date.parse(valid[i].timestamp)).filter(g => g > 0).sort((a, b) => a - b)
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0
  const span = Math.max(1, to - from)
  const count = median > 0 ? Math.max(24, Math.min(96, Math.round(span / median))) : 96
  const width = span / count
  const slots = Array.from({ length: count }, (_, i) => ({ start: from + i * width, end: from + (i + 1) * width, up: 0, down: 0 }))
  for (const p of valid) {
    const fraction = p.uptime_pct != null && Number.isFinite(p.uptime_pct)
      ? Math.max(0, Math.min(1, p.uptime_pct / 100)) : p.is_up == null ? null : p.is_up ? 1 : 0
    const samples = p.sample_count ?? 1
    if (fraction == null || !Number.isFinite(samples) || samples <= 0) continue
    const slot = slots[Math.min(count - 1, Math.floor((Date.parse(p.timestamp) - from) / width))]
    slot.up += fraction * samples
    slot.down += (1 - fraction) * samples
  }
  return slots.map(s => ({ ...s,
    state: s.up + s.down === 0 ? 'gap' as const : s.down === 0 ? 'up' as const : s.up === 0 ? 'down' as const : 'partial' as const,
    pct: s.up + s.down > 0 ? s.up * 100 / (s.up + s.down) : null,
  }))
}
