/**
 * Formatting rules for the Availability page.
 *
 * - Availability is always two decimals and truncated (never rounded up), so a
 *   99.899% service never reads "99.90%" next to a 99.9% target, and a
 *   non-perfect value never reads "100.00%" (capped at 99.99).
 * - `null` is NOT MEASURED — never rendered as 0.
 * - Differences between percentages are percentage points ("pp").
 */

export const NOT_MEASURED = 'Not measured'

/** Truncate to two decimals; a non-perfect measurement never shows 100.00. */
export function availNumber(v: number): number {
  if (v >= 100) return 100
  const truncated = Math.floor(v * 100 + 1e-7) / 100
  return Math.min(truncated, 99.99)
}

export function fmtAvail(v: number | null | undefined, empty = NOT_MEASURED): string {
  if (v == null || Number.isNaN(v)) return empty
  return `${availNumber(v).toFixed(2)}%`
}

/** Configured targets are shown as configured (99.9%, 99.95%, 95%). */
export function fmtTarget(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${Number(v.toFixed(3))}%`
}

/** Generic share (coverage, utilisation) — one decimal, not an availability. */
export function fmtShare(v: number | null | undefined, empty = '—'): string {
  if (v == null || Number.isNaN(v)) return empty
  if (v < 100 && v >= 99.95) return '99.9%'
  return `${v.toFixed(1)}%`
}

/** Signed percentage-point change; null means no comparable prior data. */
export function fmtPp(v: number | null | undefined): string | null {
  if (v == null || Number.isNaN(v)) return null
  const rounded = Math.round(v * 100) / 100
  if (rounded === 0) return '±0.00 pp'
  return `${rounded > 0 ? '+' : '−'}${Math.abs(rounded).toFixed(2)} pp`
}

/** Minutes → human duration. null → "Unknown" (never 0). */
export function fmtDuration(min: number | null | undefined, empty = 'Unknown'): string {
  if (min == null || Number.isNaN(min)) return empty
  if (min <= 0) return '0 min'
  if (min < 1) return `${Math.max(1, Math.round(min * 60))}s`
  if (min < 60) return `${min < 10 ? min.toFixed(1).replace(/\.0$/, '') : Math.round(min)} min`
  const totalMin = Math.round(min)
  if (totalMin < 24 * 60) {
    const h = Math.floor(totalMin / 60)
    const m = totalMin % 60
    return m ? `${h}h ${m}m` : `${h}h`
  }
  const d = Math.floor(totalMin / 1440)
  const h = Math.floor((totalMin % 1440) / 60)
  return h ? `${d}d ${h}h` : `${d}d`
}

export function fmtCount(v: number | null | undefined, empty = '—'): string {
  if (v == null || Number.isNaN(v)) return empty
  return v.toLocaleString()
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

/** Browser timezone name + UTC offset, e.g. "Asia/Riyadh (UTC+3)". */
export function timezoneLabel(): string {
  let name = 'Local time'
  try {
    name = Intl.DateTimeFormat().resolvedOptions().timeZone || name
  } catch { /* older browsers */ }
  const offsetMin = -new Date().getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '−'
  const abs = Math.abs(offsetMin)
  const hh = Math.floor(abs / 60)
  const mm = abs % 60
  return `${name} (UTC${sign}${hh}${mm ? `:${String(mm).padStart(2, '0')}` : ''})`
}

/** Parse backend timestamps; naive values are UTC. */
export function parseTs(iso: string | null | undefined): number | null {
  if (!iso) return null
  let s = iso
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = `${s.replace(' ', 'T')}Z`
  const t = Date.parse(s)
  return Number.isNaN(t) ? null : t
}

export function fmtDateTime(ms: number | null | undefined, withSeconds = false): string {
  if (ms == null) return '—'
  return new Date(ms).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
  })
}

export function fmtClock(ms: number | null | undefined, withSeconds = false): string {
  if (ms == null) return '—'
  return new Date(ms).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
  })
}

export function fmtPeriod(fromMs: number, toMs: number): string {
  const sameYear = new Date(fromMs).getFullYear() === new Date(toMs).getFullYear()
  const opts: Intl.DateTimeFormatOptions = {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    ...(sameYear ? {} : { year: 'numeric' as const }),
  }
  return `${new Date(fromMs).toLocaleString([], opts)} → ${new Date(toMs).toLocaleString([], opts)}`
}

/** "3m ago" style age relative to a reference instant. */
export function fmtAge(ms: number | null | undefined, nowMs = Date.now()): string {
  if (ms == null) return 'never'
  const diff = Math.max(0, (nowMs - ms) / 1000)
  if (diff < 45) return 'just now'
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

/** Absolute skew as "12h 3m" / "4 min". */
export function fmtSkew(ms: number): string {
  const min = Math.round(Math.abs(ms) / 60_000)
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  const m = min % 60
  return m ? `${h}h ${m}m` : `${h}h`
}

/** Rolling preset labels spelled out for management readers. */
export const PRESET_LABELS: Record<string, string> = {
  '1h': 'Last hour',
  '24h': 'Last 24 hours',
  '7d': 'Last 7 days',
  '1M': 'Last 30 days',
}

export const PRESET_BUTTON_LABELS = {
  '1M': 'Last 30 days',
} as const
