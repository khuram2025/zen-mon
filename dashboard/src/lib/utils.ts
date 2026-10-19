import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { serverNow } from './serverClock'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Unit index for `value` in a 1000/1024-step scale, clamped to [0, top]. Values in (0, 1) have a
 * negative logarithm; unclamped they indexed `units[-1]` and printed "500.00 undefined" (X-51).
 */
function unitIndex(value: number, k: number, top: number): number {
  const a = Math.abs(value)
  if (!(a >= 1)) return 0
  return Math.max(0, Math.min(Math.floor(Math.log(a) / Math.log(k)), top))
}

export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0 || !bytes || !Number.isFinite(bytes)) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  if (Math.abs(bytes) < 1) return `${bytes < 0 ? '-' : ''}<1 B`
  const i = unitIndex(bytes, k, sizes.length - 1)
  return `${(bytes / Math.pow(k, i)).toFixed(decimals)} ${sizes[i]}`
}

export function formatBps(bps: number): string {
  if (!bps || bps === 0 || !Number.isFinite(bps)) return '0 bps'
  const k = 1000
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  if (Math.abs(bps) < 1) return `${bps < 0 ? '-' : ''}<1 bps`
  const i = unitIndex(bps, k, units.length - 1)
  return `${(bps / Math.pow(k, i)).toFixed(2)} ${units[i]}`
}

/** Compact bps for chart axes. formatBps' two decimals ("315.46 Kbps") overflow
 *  a narrow y-axis gutter and get clipped mid-number. Fractional ticks below 1
 *  are real on quiet series and print as numbers, never "undefined". */
export function formatBpsAxis(bps: number): string {
  if (!bps || !Number.isFinite(bps)) return '0'
  const units = ['', 'K', 'M', 'G', 'T']
  if (Math.abs(bps) < 1) return String(Number(bps.toFixed(2)))
  const i = unitIndex(bps, 1000, units.length - 1)
  const v = bps / Math.pow(1000, i)
  return `${v.toFixed(v < 10 ? 1 : 0)}${units[i]}`
}

/** Evenly spaced ticks across a time window. Recharts derives ticks from the
 *  data, so a range whose samples cluster in one corner gets a single label
 *  and no sense of scale. Wide "Jul 29, 10:55 PM" labels fit 4, not 5. */
export function timeTicks(fromTs: number, toTs: number, rangeHours: number): number[] {
  if (!Number.isFinite(fromTs) || !Number.isFinite(toTs) || toTs <= fromTs) return []
  const count = rangeHours > 12 && rangeHours <= 24 * 7 ? 4 : 5
  const step = (toTs - fromTs) / (count - 1)
  return Array.from({ length: count }, (_, i) => Math.round(fromTs + i * step))
}

/** Right margin so the final axis label, centred on the plot's right edge,
 *  isn't clipped by the SVG. */
export function axisRightPad(rangeHours: number): number {
  return rangeHours > 12 && rangeHours <= 24 * 7 ? 46 : 20
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds < 0) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${Math.floor(seconds)}s`
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never'
  // Backend timestamps are always UTC. Most carry an explicit offset (Z / +00:00),
  // but guard against any naive value (no T/Z/offset) being parsed as browser-local
  // time — which would skew "x ago" by the viewer's timezone. Treat naive as UTC.
  let s = iso
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)
  if (!hasTz) s = s.replace(' ', 'T') + 'Z'
  const then = new Date(s).getTime()
  if (isNaN(then)) return '—'
  const diff = (serverNow() - then) / 1000
  if (diff < 0) return 'just now'          // clock skew: don't show "future"
  if (diff < 10) return 'just now'
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

/**
 * Range-aware tick formatter for time-series charts.
 * - ≤ 24h: HH:mm
 * - ≤ 7d:  "Mon DD HH"
 * - > 7d:  "Mon DD"
 * Pass the active range in hours; pass `ts` (epoch ms) when calling.
 */
export function timeAxisTickFormatter(rangeHours: number): (ts: number) => string {
  if (rangeHours <= 24) {
    return (ts: number) =>
      new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (rangeHours <= 24 * 7) {
    return (ts: number) => {
      const d = new Date(ts)
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
        ' ' + d.toLocaleTimeString([], { hour: '2-digit' })
    }
  }
  return (ts: number) =>
    new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Tooltip label formatter — always shows day + time so hovered points
 *  in multi-day ranges have full context. */
export function timeTooltipLabelFormatter(ts: any): string {
  return new Date(ts).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

/** One field error from an API response: `path` like `conditions[0].operator` ('' = the request as a whole). */
export type ApiFieldError = { path: string; msg: string }

/** A pydantic `loc` as a field path: ['body','conditions',0,'operator'] -> 'conditions[0].operator'. */
function locPath(loc: unknown): string {
  if (!Array.isArray(loc)) return typeof loc === 'string' ? loc : ''
  const parts = loc[0] === 'body' || loc[0] === 'query' || loc[0] === 'path' ? loc.slice(1) : loc
  let out = ''
  for (const p of parts) {
    if (typeof p === 'number' || /^\d+$/.test(String(p))) out += `[${p}]`
    else out += out ? `.${p}` : String(p)
  }
  return out
}

/** A long regex ("String should match pattern '^(ping_status|rtt|…)$'") is never shown; the value is. */
function readableMsg(msg: string, input?: unknown): string {
  let m = String(msg || '').replace(/^Value error,\s*/i, '')
  if (/should match pattern/i.test(m) || /'\^\(.{40,}/.test(m)) {
    return input !== undefined && input !== null && typeof input !== 'object'
      ? `'${String(input).slice(0, 80)}' is not an accepted value`
      : 'is not an accepted value'
  }
  if (m.length > 300) m = `${m.slice(0, 297)}…`
  return m
}

/** Every field error in an API failure: pydantic 422 arrays, {loc,msg} objects and plain detail strings. */
export function apiFieldErrors(e: any): ApiFieldError[] {
  const d = e?.response?.data?.detail
  if (Array.isArray(d)) {
    return d.map((x: any) => {
      if (!x || typeof x !== 'object') return { path: '', msg: readableMsg(String(x)) }
      const path = locPath(x.loc)
      let msg = readableMsg(x.msg ?? x.message ?? '', x.input)
      // A server that already names the field ("metric: 'x' is not an accepted value") is not prefixed twice.
      const leaf = path.split(/[.[]/).pop()?.replace(/\]$/, '') ?? ''
      for (const prefix of [path, leaf]) if (prefix && msg.startsWith(`${prefix}: `)) { msg = msg.slice(prefix.length + 2); break }
      return { path, msg }
    }).filter((x) => x.msg)
  }
  if (typeof d === 'string' && d) return [{ path: '', msg: readableMsg(d) }]
  if (d && typeof d === 'object') {
    if (Array.isArray(d.errors)) return apiFieldErrors({ response: { data: { detail: d.errors } } })
    const msg = d.msg ?? d.message
    if (msg) return [{ path: locPath(d.loc) || (typeof d.field === 'string' ? d.field : ''), msg: readableMsg(String(msg), d.input) }]
  }
  if (e?.message) return [{ path: '', msg: String(e.message) }]
  return []
}

export function apiErrorMessage(e: any, fallback = 'Something went wrong'): string {
  const errs = apiFieldErrors(e)
  if (!errs.length) return fallback
  // 'metric: ‘x’ is not an accepted value; conditions[0].operator: …' — the field path, never the regex.
  return errs.map((x) => (x.path ? `${x.path}: ${x.msg}` : x.msg)).join('; ')
}

/**
 * Copy text to the clipboard, falling back to a hidden textarea + execCommand.
 *
 * The async Clipboard API is gated on a secure context, so on an appliance reached over
 * plain HTTP at a LAN address (http://192.168.x.x) `navigator.clipboard` is undefined
 * outright. Call sites that only used it silently copied nothing while still reporting
 * success. Returns whether the text actually reached the clipboard so callers can say so.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Blocked or permission denied — fall through to the legacy path.
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    // execCommand needs a rendered, selectable node; keep it off-screen without display:none.
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, ta.value.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}
