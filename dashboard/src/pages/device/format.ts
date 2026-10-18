import { serverNow } from '@/lib/serverClock'

/* Formatting rules for the device page (one place, so every tab agrees):
 *  - one display timezone (the browser's), named once in the range bar;
 *  - one absolute format and one relative format; relative stamps carry the
 *    absolute time for hover;
 *  - numbers to three significant figures with auto-scaled units, always with
 *    the unit; availability keeps two decimals, CPU/memory whole percents;
 *  - missing values are "—", never zero. */

export const MISSING = '—'

export function displayTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time'
  } catch {
    return 'local time'
  }
}

/** Backend timestamps are UTC; a naive value (no offset) is read as UTC, not local time. */
export function parseTime(value: string | number | Date | null | undefined): number | null {
  if (value == null || value === '') return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.getTime() : null
  let s = value
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = s.replace(' ', 'T') + 'Z'
  const ms = Date.parse(s)
  return Number.isFinite(ms) ? ms : null
}

const ABSOLUTE: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }

/** "Oct 18, 2026, 02:16 PM" in the display timezone; `seconds` adds :ss. */
export function formatAbsolute(value: string | number | Date | null | undefined, opts: { seconds?: boolean } = {}): string {
  const ms = parseTime(value)
  if (ms == null) return MISSING
  return new Date(ms).toLocaleString(undefined, opts.seconds ? { ...ABSOLUTE, second: '2-digit' } : ABSOLUTE)
}

/** "just now", "5m ago", "3h 12m ago", "2d 22h ago" against the appliance clock. */
export function formatRelative(value: string | number | Date | null | undefined, nowMs = serverNow()): string {
  const ms = parseTime(value)
  if (ms == null) return MISSING
  const s = Math.max(0, (nowMs - ms) / 1000)
  if (s < 10) return 'just now'
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) {
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return m ? `${h}h ${m}m ago` : `${h}h ago`
  }
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  return h ? `${d}d ${h}h ago` : `${d}d ago`
}

/** Compact span: "45s", "8m", "3h 12m", "2d 21h". */
export function formatSpan(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return MISSING
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    return m ? `${h}h ${m}m` : `${h}h`
  }
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  return h ? `${d}d ${h}h` : `${d}d`
}

/** Three significant figures, grouped thousands from 1000 up: 0.123, 1.23, 12.3, 123, 1,234. */
export function sig3(value: number): string {
  if (!Number.isFinite(value)) return MISSING
  const abs = Math.abs(value)
  if (abs >= 1000) return Math.round(value).toLocaleString()
  if (abs === 0) return '0'
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3
  return String(Number(value.toFixed(digits)))
}

function scaled(value: number, base: number, units: string[]): string {
  if (!Number.isFinite(value)) return MISSING
  let i = 0
  let v = Math.abs(value)
  while (v >= base && i < units.length - 1) { v /= base; i++ }
  return `${value < 0 ? '-' : ''}${sig3(v)} ${units[i]}`
}

/** 24.0 Mbps, 160 Mbps, 1.23 Gbps — always with the unit. */
export function formatBitRate(bps: number | null | undefined): string {
  if (bps == null || !Number.isFinite(bps)) return MISSING
  if (bps > 0 && bps < 1) return '<1 bps'
  return scaled(bps, 1000, ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'])
}

/** Compact axis label: "160M", "1.5G", "800" — no decimals beyond what is needed. */
export function formatBitRateAxis(bps: number): string {
  if (!Number.isFinite(bps) || bps === 0) return '0'
  const units = ['', 'K', 'M', 'G', 'T']
  let i = 0
  let v = Math.abs(bps)
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  const shown = v >= 10 ? Math.round(v).toString() : String(Number(v.toFixed(1)))
  return `${bps < 0 ? '-' : ''}${shown}${units[i]}`
}

/** Bytes, base 1024: 15 GB, 440 GB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return MISSING
  return scaled(bytes, 1024, ['B', 'KB', 'MB', 'GB', 'TB', 'PB'])
}

/** Megabytes as reported by a device ("15,352 MB") converted to a readable size. */
export function formatMegabytes(mb: number | null | undefined): string {
  return mb == null ? MISSING : formatBytes(mb * 1024 * 1024)
}

/** Interface speed: 100 Mbps, 1 Gbps, 10 Gbps. */
export function formatLinkSpeed(bps: number | string | null | undefined): string {
  const n = Number(bps)
  if (!n || !Number.isFinite(n)) return MISSING
  return formatBitRate(n).replace(/\.0+ /, ' ')
}

/** CPU / memory / utilisation: whole percent ("45%"), "<1%" for live but tiny. */
export function formatUsagePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return MISSING
  if (value > 0 && value < 0.5) return '<1%'
  return `${Math.round(value)}%`
}

/** Loss keeps one decimal: 1.1%. */
export function formatLossPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return MISSING
  return `${value === 0 ? '0' : value.toFixed(1)}%`
}

export function formatMs(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return MISSING
  return `${sig3(value)} ms`
}

/** A reading with its unit; unit-less sensor values get a unit inferred from the label. */
export function formatReading(value: number | null | undefined, unit?: string | null, label?: string): string {
  if (value == null || !Number.isFinite(value)) return MISSING
  const u = (unit || inferUnit(label) || '').trim()
  const lu = u.toLowerCase()
  if (lu === 'bps') return formatBitRate(value)
  if (lu === 'kbps') return formatBitRate(value * 1000)
  if (lu === 'bytes' || lu === 'b') return formatBytes(value)
  if (lu === 'mb') return formatMegabytes(value)
  if (lu === '%') return `${sig3(value)}%`
  // "1 tunnels" → "1 tunnel"; units that are counted nouns read as words.
  const shown = value === 1 && /^[a-z]{3,}s$/i.test(u) && !/^(bps|ms|rpm)$/i.test(u) ? u.slice(0, -1) : u
  return shown ? `${sig3(value)} ${shown}` : sig3(value)
}

/** Unit for a sensor whose template value has none, from its name: "Temp 1" → °C, "FAN1" → RPM, "VCC 3.3V" → V. */
export function inferUnit(label?: string | null): string | null {
  const s = (label || '').toLowerCase()
  if (!s) return null
  if (/temp|thermal|°c|celsius|\bdts\b|cpu.*(on.?die|core)/.test(s)) return '°C'
  if (/\bfan\b|fan\s*\d|fan_|rpm/.test(s)) return 'RPM'
  if (/volt|vcc|vdd|vbat|\b\d+(\.\d+)?\s*v\b|\bp\d+v\d*/.test(s)) return 'V'
  if (/\bpsu\b|power supply/.test(s)) return null
  return null
}

/* ── Trend: an absolute delta against a named baseline ─────────────────── */

export type TrendKind = 'percent' | 'latency' | 'loss' | 'count'

export type Trend = {
  /** "+0.1 pt", "−22 ms" */
  text: string
  /** True when the change is large enough to colour (significance band or a threshold crossed). */
  significant: boolean
  direction: 'up' | 'down' | 'flat'
  /** What the change is measured against, for the label: "vs 1h average". */
  baseline: string
}

/** Latest value against the average of the selected range. Coloured only when it
 *  moves past a band that matters for the metric (5 pt for CPU/memory, 1 pt of
 *  loss, max(10 ms, 25%) of latency) or crosses a configured threshold. */
export function trendOf(values: Array<number | null>, kind: TrendKind, rangeLabel: string, threshold?: number | null): Trend | null {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (xs.length < 4) return null
  const latest = xs[xs.length - 1]
  const mean = xs.reduce((s, v) => s + v, 0) / xs.length
  const delta = latest - mean
  const band = kind === 'percent' ? 5 : kind === 'loss' ? 1 : kind === 'latency' ? Math.max(10, mean * 0.25) : Math.max(1, Math.abs(mean) * 0.25)
  const crossed = threshold != null && ((mean < threshold) !== (latest < threshold))
  const unit = kind === 'latency' ? ' ms' : kind === 'count' ? '' : ' pt'
  const rounded = kind === 'latency' ? Math.round(delta) : kind === 'count' ? Math.round(delta) : Number(delta.toFixed(1))
  const direction = rounded === 0 ? 'flat' : rounded > 0 ? 'up' : 'down'
  const sign = rounded > 0 ? '+' : rounded < 0 ? '−' : '±'
  const magnitude = kind === 'count' ? Math.abs(rounded).toLocaleString() : String(Math.abs(rounded))
  return {
    text: `${sign}${magnitude}${unit}`,
    significant: direction !== 'flat' && (Math.abs(delta) >= band || crossed),
    direction,
    baseline: `vs ${rangeLabel.replace(/^Last\s+/i, '')} average`,
  }
}

/* ── Chart domains ──────────────────────────────────────────────────────── */

/** Percent metrics on a fixed 0–100 scale, so a 0.1-point wobble stays flat. */
export const PERCENT_DOMAIN: [number, number] = [0, 100]

/** Latency from 0 to the larger of the limit and the 99th percentile. */
export function latencyDomain(values: Array<number | null>, limit?: number | null): [number, number] {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v)).sort((a, b) => a - b)
  const p99 = xs.length ? xs[Math.min(xs.length - 1, Math.floor(xs.length * 0.99))] : 0
  const top = Math.max(limit ?? 0, p99, 1)
  return [0, top * 1.1]
}

/** A domain at least `span` wide around the data, for metrics without a natural scale. */
export function minimumSpanDomain(values: Array<number | null>, span: number): [number, number] {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (!xs.length) return [0, span]
  const lo = Math.min(...xs)
  const hi = Math.max(...xs)
  if (hi - lo >= span) return [lo, hi]
  const mid = (lo + hi) / 2
  const low = Math.max(0, mid - span / 2)
  return [low, low + span]
}

/* ── Statistics ─────────────────────────────────────────────────────────── */

export type Stats = { min: number; avg: number; p95: number; max: number; count: number }

export function statsOf(values: Array<number | null>): Stats | null {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v)).sort((a, b) => a - b)
  if (!xs.length) return null
  return {
    min: xs[0],
    max: xs[xs.length - 1],
    avg: xs.reduce((s, v) => s + v, 0) / xs.length,
    p95: xs[Math.min(xs.length - 1, Math.ceil(xs.length * 0.95) - 1)],
    count: xs.length,
  }
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}
