/**
 * NetFlow display formatting.
 *
 * Traffic uses decimal SI units throughout (1 kB = 1000 B, 1 Mbps = 10^6 bit/s): the review found
 * byte volumes scaled by 1024 under "GB"/"MB" labels, so the same traffic read ~7% differently on
 * the byte and the bit-rate widgets. Every formatter returns "—" for a missing value: unknown is
 * never rendered as zero. Exact values for titles/tooltips come from the *Exact helpers.
 */

const NUMBER_LOCALE = 'en-US'
const DASH = '—'

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

const BYTE_UNITS = ['B', 'kB', 'MB', 'GB', 'TB', 'PB', 'EB']
const BIT_RATE_UNITS = ['bps', 'kbps', 'Mbps', 'Gbps', 'Tbps', 'Pbps']
const AXIS_SUFFIXES = ['', 'k', 'M', 'G', 'T', 'P']

/**
 * Scale by 1000 into `units`. Values in (0, 1) render as "<1 unit" rather than a unit index of -1
 * (which printed "500.00 undefined" for 0.5 bps), and a value that rounds up to 1000 carries into
 * the next unit ("1.00 MB", not "1000 kB").
 */
function scaleDecimal(value: number, units: string[]): string {
  if (value === 0) return `0 ${units[0]}`
  const sign = value < 0 ? '-' : ''
  let a = Math.abs(value)
  if (a < 1) return `${sign}<1 ${units[0]}`
  let i = 0
  while (a >= 1000 && i < units.length - 1) {
    a /= 1000
    i++
  }
  const digits = (v: number) => (i === 0 && Number.isInteger(v) ? 0 : v < 10 ? 2 : v < 100 ? 1 : 0)
  let text = a.toFixed(digits(a))
  if (Number(text) >= 1000 && i < units.length - 1) {
    a /= 1000
    i++
    text = a.toFixed(digits(a))
  }
  return `${sign}${text} ${units[i]}`
}

/** Byte volume, e.g. "19.06 GB". */
export function formatBytes(bytes: number | null | undefined): string {
  return isNum(bytes) ? scaleDecimal(bytes, BYTE_UNITS) : DASH
}

/** Exact byte count for titles, e.g. "19,057,630,938 bytes". */
export function formatBytesExact(bytes: number | null | undefined): string {
  return isNum(bytes) ? `${formatCountExact(bytes)} bytes` : DASH
}

/** Bit rate, e.g. "39.09 Mbps"; sub-1 rates read "<1 bps". */
export function formatBps(bps: number | null | undefined): string {
  return isNum(bps) ? scaleDecimal(bps, BIT_RATE_UNITS) : DASH
}

/** Exact bit rate for titles, e.g. "39,090,112 bit/s". */
export function formatBpsExact(bps: number | null | undefined): string {
  return isNum(bps) ? `${formatCountExact(Math.round(bps))} bit/s` : DASH
}

/**
 * Compact axis label ("1.5k", "12M", "0.5"): the unit belongs on the axis title. Fractional ticks
 * below 1 are real on quiet filters and long buckets, so they print as numbers, not "undefined".
 */
export function formatBpsAxis(bps: number | null | undefined): string {
  if (!isNum(bps)) return ''
  if (bps === 0) return '0'
  const sign = bps < 0 ? '-' : ''
  let a = Math.abs(bps)
  if (a < 1000) return `${sign}${trimNumber(a, a < 10 ? 2 : 0)}`
  let i = 0
  while (a >= 1000 && i < AXIS_SUFFIXES.length - 1) {
    a /= 1000
    i++
  }
  return `${sign}${trimNumber(a, a < 10 ? 1 : 0)}${AXIS_SUFFIXES[i]}`
}

function trimNumber(v: number, maxDigits: number): string {
  return String(Number(v.toFixed(maxDigits)))
}

/** Exact count with grouping, e.g. "1,514,234". */
export function formatCountExact(n: number | null | undefined): string {
  return isNum(n) ? n.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: 0 }) : DASH
}

const COMPACT_FROM = 1_000_000

/**
 * Count that stays legible in a narrow KPI: exact below one million, compact above
 * ("1.5M", "9.9B"). Pair it with `formatCountExact` in a title so the precise value is one hover
 * (or one screen-reader label) away.
 */
export function formatCount(n: number | null | undefined): string {
  if (!isNum(n)) return DASH
  if (Math.abs(n) < COMPACT_FROM) return formatCountExact(n)
  return new Intl.NumberFormat(NUMBER_LOCALE, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
}

/** A percentage 0..100 ("12.5%", "<0.1%", "100%"). */
export function formatPercent(pct: number | null | undefined, digits = 1): string {
  if (!isNum(pct)) return DASH
  if (pct !== 0 && Math.abs(pct) < 0.1) return pct < 0 ? '>-0.1%' : '<0.1%'
  return `${trimNumber(pct, digits)}%`
}

function durationPart(v: number): string {
  return v < 10 ? String(Math.round(v * 10) / 10) : String(Math.round(v))
}

/**
 * Duration from milliseconds, escalating ms → s → min → h → d: 500 → "500 ms", 1500 → "1.5 s",
 * 60000 → "60 s", 90000 → "1.5 min". Negative or missing durations are unknown ("—").
 */
export function formatDuration(ms: number | null | undefined): string {
  if (!isNum(ms) || ms < 0) return DASH
  if (ms === 0) return '0 ms'
  if (ms < 1) return '<1 ms'
  if (ms < 999.5) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s <= 60) return `${durationPart(s)} s`
  const min = s / 60
  if (min <= 60) return `${durationPart(min)} min`
  const h = min / 60
  if (h <= 48) return `${durationPart(h)} h`
  return `${durationPart(h / 24)} d`
}

/**
 * A bucket or interval length in its largest whole unit: 300 → "5 min", 3600 → "1 h",
 * 86400 → "1 d", 90 → "90 s". Used for granularity labels, where "60 min" would read oddly.
 */
export function formatInterval(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds <= 0) return DASH
  if (seconds % 86_400 === 0) return `${seconds / 86_400} d`
  if (seconds % 3_600 === 0) return `${seconds / 3_600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${trimNumber(seconds, 1)} s`
}

/**
 * Address for display. The API returns IPv4 flows as plain dotted quads, but IPv4-mapped IPv6
 * text (`::ffff:10.0.0.1`, or its hex form `::ffff:a00:1`) can still reach the UI from unified
 * address columns; it is shown as the IPv4 address. `::` is the schema's "no address" sentinel.
 */
export function ipDisplay(ip: string | null | undefined): string {
  if (ip == null) return DASH
  const v = String(ip).trim()
  if (!v || v === '::') return DASH
  const dotted = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})(\/\d+)?$/i.exec(v)
  if (dotted) return dotted[1] + (dotted[2] ? `/${Math.max(0, Number(dotted[2].slice(1)) - 96)}` : '')
  const hex = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/i.exec(v)
  if (hex) {
    const hi = parseInt(hex[1], 16)
    const lo = parseInt(hex[2], 16)
    return `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`
  }
  return v
}

/* ─── Time ─────────────────────────────────────────────────────────── */

/** True when `tz` is an IANA zone this browser can format in. */
export function isValidTimeZone(tz: string | null | undefined): tz is string {
  if (!tz || !/^[A-Za-z][A-Za-z0-9_+\-]*(?:\/[A-Za-z0-9_+\-]+)*$/.test(tz)) return false
  try {
    new Intl.DateTimeFormat(NUMBER_LOCALE, { timeZone: tz })
    return true
  } catch {
    return false
  }
}

function toMs(value: string | number | Date): number {
  if (value instanceof Date) return value.getTime()
  if (typeof value === 'number') return value
  return Date.parse(value)
}

interface ZonedParts {
  year: string
  month: string
  day: string
  hour: string
  minute: string
  second: string
}

function zonedParts(ms: number, tz: string): ZonedParts {
  const parts = new Intl.DateTimeFormat(NUMBER_LOCALE, {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(ms))
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00'
  return { year: get('year'), month: get('month'), day: get('day'), hour: get('hour'), minute: get('minute'), second: get('second') }
}

/** UTC offset of `tz` at an instant, e.g. "UTC+03:00", "UTC−05:30". */
export function tzOffsetLabel(tz: string, at: string | number | Date = Date.now()): string {
  const ms = toMs(at)
  const zone = isValidTimeZone(tz) ? tz : 'UTC'
  if (!Number.isFinite(ms)) return 'UTC'
  const p = zonedParts(ms, zone)
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second)
  const offMin = Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 60_000)
  if (offMin === 0) return 'UTC'
  const abs = Math.abs(offMin)
  return `UTC${offMin > 0 ? '+' : '−'}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`
}

export interface DateTimeOptions {
  /** Include seconds (default true). */
  seconds?: boolean
  /** Append the zone: 'offset' → "UTC+03:00", 'name' → "Asia/Riyadh", 'none'. Default 'offset'. */
  zone?: 'offset' | 'name' | 'none'
  /** Omit the date part (time of day only). */
  timeOnly?: boolean
}

/**
 * An exact, locale-neutral timestamp in `tz`: "2026-09-30 14:03:22 UTC+03:00". Forensics and
 * evidence need an unambiguous primary time; relative ages are secondary.
 */
export function formatDateTime(
  value: string | number | Date | null | undefined,
  tz: string,
  opts: DateTimeOptions = {},
): string {
  if (value == null || value === '') return DASH
  const ms = toMs(value)
  if (!Number.isFinite(ms)) return DASH
  const zone = isValidTimeZone(tz) ? tz : 'UTC'
  const p = zonedParts(ms, zone)
  const time = `${p.hour}:${p.minute}${opts.seconds === false ? '' : `:${p.second}`}`
  const text = opts.timeOnly ? time : `${p.year}-${p.month}-${p.day} ${time}`
  const mode = opts.zone ?? 'offset'
  if (mode === 'none') return text
  return `${text} ${mode === 'name' ? zone : tzOffsetLabel(zone, ms)}`
}

/** Calendar date of an instant in `tz` ("2026-09-30"), for same-day checks in labels. */
export function zonedDate(value: string | number | Date, tz: string): string {
  const ms = toMs(value)
  if (!Number.isFinite(ms)) return ''
  const p = zonedParts(ms, isValidTimeZone(tz) ? tz : 'UTC')
  return `${p.year}-${p.month}-${p.day}`
}

/* ─── Names ────────────────────────────────────────────────────────── */

const PROTOCOL_NAMES: Record<number, string> = {
  1: 'ICMP',
  2: 'IGMP',
  4: 'IP-in-IP',
  6: 'TCP',
  17: 'UDP',
  41: 'IPv6-in-IPv4',
  47: 'GRE',
  50: 'ESP',
  51: 'AH',
  58: 'ICMPv6',
  88: 'EIGRP',
  89: 'OSPF',
  103: 'PIM',
  112: 'VRRP',
  115: 'L2TP',
  132: 'SCTP',
}

/** IANA protocol name when well known, else null. */
export function protocolName(n: number | null | undefined): string | null {
  return isNum(n) ? PROTOCOL_NAMES[n] ?? null : null
}

/** "TCP (6)" or "Protocol 253". */
export function protocolLabel(n: number | null | undefined): string {
  if (!isNum(n)) return DASH
  const name = PROTOCOL_NAMES[n]
  return name ? `${name} (${n})` : `Protocol ${n}`
}

const DSCP_NAMES: Record<number, string> = {
  0: 'CS0 / Best effort',
  8: 'CS1',
  10: 'AF11',
  12: 'AF12',
  14: 'AF13',
  16: 'CS2',
  18: 'AF21',
  20: 'AF22',
  22: 'AF23',
  24: 'CS3',
  26: 'AF31',
  28: 'AF32',
  30: 'AF33',
  32: 'CS4',
  34: 'AF41',
  36: 'AF42',
  38: 'AF43',
  40: 'CS5',
  44: 'Voice-Admit',
  46: 'EF',
  48: 'CS6',
  56: 'CS7',
}

/** "EF (46)" or "DSCP 13". */
export function dscpLabel(n: number | null | undefined): string {
  if (!isNum(n)) return DASH
  const name = DSCP_NAMES[n]
  return name ? `${name} (${n})` : `DSCP ${n}`
}

/** ISO weekday as ClickHouse `toDayOfWeek` numbers it: 1 = Monday … 7 = Sunday. */
const DOW_NAMES = ['', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

export function dowName(n: number | null | undefined, short = false): string {
  if (!isNum(n) || n < 1 || n > 7) return DASH
  return short ? DOW_NAMES[n].slice(0, 3) : DOW_NAMES[n]
}

/** "22:00–22:59" for an hour-of-day bucket. */
export function hourRangeLabel(hour: number | null | undefined): string {
  if (!isNum(hour) || hour < 0 || hour > 23) return DASH
  const hh = String(hour).padStart(2, '0')
  return `${hh}:00–${hh}:59`
}
