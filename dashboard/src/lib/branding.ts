/**
 * Organisation branding and regional formatting (Settings → General → Company).
 *
 * Mirrors server/app/services/branding.py so on-screen reports print dates,
 * times and the zone label exactly as the PDF/HTML/email renderers do.
 */

export type DateFormat = 'YYYY-MM-DD' | 'DD/MM/YYYY' | 'MM/DD/YYYY' | 'DD.MM.YYYY' | 'DD-MMM-YYYY' | 'MMM D, YYYY'
export type TimeFormat = '24h' | '12h'
export type Confidentiality = '' | 'Public' | 'Internal' | 'Confidential' | 'Restricted'

export interface Branding {
  company_name: string
  display_name: string
  company_address: string
  company_email: string
  company_phone: string
  company_website: string
  timezone: string
  date_format: DateFormat
  time_format: TimeFormat
  week_start: 'monday' | 'sunday' | 'saturday'
  brand_color: string
  report_footer: string
  report_disclaimer: string
  confidentiality: Confidentiality
  paper_size: 'A4' | 'Letter'
  has_logo: boolean
  has_logo_dark: boolean
  logo_version: string
  logo_dark_version: string
}

export const DEFAULT_BRAND_COLOR = '#4F6BF6'

export const DEFAULT_BRANDING: Branding = {
  company_name: '',
  display_name: 'ZenPlus',
  company_address: '',
  company_email: '',
  company_phone: '',
  company_website: '',
  timezone: 'UTC',
  date_format: 'YYYY-MM-DD',
  time_format: '24h',
  week_start: 'monday',
  brand_color: DEFAULT_BRAND_COLOR,
  report_footer: '',
  report_disclaimer: '',
  confidentiality: '',
  paper_size: 'A4',
  has_logo: false,
  has_logo_dark: false,
  logo_version: '',
  logo_dark_version: '',
}

export const DATE_FORMATS: { value: DateFormat; example: string }[] = [
  { value: 'YYYY-MM-DD', example: '2026-10-07' },
  { value: 'DD/MM/YYYY', example: '07/10/2026' },
  { value: 'MM/DD/YYYY', example: '10/07/2026' },
  { value: 'DD.MM.YYYY', example: '07.10.2026' },
  { value: 'DD-MMM-YYYY', example: '07-Oct-2026' },
  { value: 'MMM D, YYYY', example: 'Oct 7, 2026' },
]

export const CONFIDENTIALITY_LEVELS: { value: Confidentiality; label: string; hint: string }[] = [
  { value: '', label: 'None', hint: 'No classification label' },
  { value: 'Public', label: 'Public', hint: 'May be shared outside the organisation' },
  { value: 'Internal', label: 'Internal', hint: 'For staff and contractors only' },
  { value: 'Confidential', label: 'Confidential', hint: 'Named recipients only' },
  { value: 'Restricted', label: 'Restricted', hint: 'Highly sensitive — strict need-to-know' },
]

export const BRAND_PRESETS = ['#4F6BF6', '#2563EB', '#0F766E', '#059669', '#7C3AED', '#C2410C', '#BE123C', '#0F172A']

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Every IANA zone the browser knows, with a static fallback for old engines. */
export function timezoneList(): string[] {
  try {
    const list = (Intl as any).supportedValuesOf?.('timeZone') as string[] | undefined
    if (list && list.length) return list.includes('UTC') ? list : ['UTC', ...list]
  } catch {
    /* fall through */
  }
  return ['UTC', 'Asia/Riyadh', 'Asia/Dubai', 'Asia/Karachi', 'Asia/Kolkata', 'Asia/Shanghai', 'Asia/Tokyo',
    'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Istanbul', 'America/New_York',
    'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney']
}

export function isValidTimezone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    return true
  } catch {
    return false
  }
}

type Parts = { year: number; month: number; day: number; hour: number; minute: number; second: number }

function toDate(value: string | number | Date): Date {
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value)
  // Naive timestamps from the API are UTC.
  const naive = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value)
  return new Date(naive ? `${value}Z` : value)
}

function zoneParts(d: Date, tz: string): Parts {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: isValidTimezone(tz) ? tz : 'UTC',
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23',
  })
  const p: Record<string, number> = {}
  for (const part of fmt.formatToParts(d)) if (part.type !== 'literal') p[part.type] = Number(part.value)
  return { year: p.year, month: p.month, day: p.day, hour: p.hour % 24, minute: p.minute, second: p.second }
}

const pad = (n: number) => String(n).padStart(2, '0')

type Fmt = Pick<Branding, 'timezone' | 'date_format' | 'time_format'>

export function formatDate(value: string | number | Date, b: Fmt): string {
  const p = zoneParts(toDate(value), b.timezone)
  switch (b.date_format) {
    case 'DD/MM/YYYY': return `${pad(p.day)}/${pad(p.month)}/${p.year}`
    case 'MM/DD/YYYY': return `${pad(p.month)}/${pad(p.day)}/${p.year}`
    case 'DD.MM.YYYY': return `${pad(p.day)}.${pad(p.month)}.${p.year}`
    case 'DD-MMM-YYYY': return `${pad(p.day)}-${MONTHS[p.month - 1]}-${p.year}`
    case 'MMM D, YYYY': return `${MONTHS[p.month - 1]} ${p.day}, ${p.year}`
    default: return `${p.year}-${pad(p.month)}-${pad(p.day)}`
  }
}

export function formatTime(value: string | number | Date, b: Fmt, seconds = false): string {
  const p = zoneParts(toDate(value), b.timezone)
  const sec = seconds ? `:${pad(p.second)}` : ''
  if (b.time_format === '12h') {
    const h = p.hour % 12 || 12
    return `${h}:${pad(p.minute)}${sec} ${p.hour < 12 ? 'AM' : 'PM'}`
  }
  return `${pad(p.hour)}:${pad(p.minute)}${sec}`
}

/**
 * Unambiguous zone label at the given instant: 'UTC', or the offset such as
 * 'GMT+3', 'GMT-4', 'GMT+5:30' — the same label the server renderers print.
 */
export function zoneLabel(tz: string, at: string | number | Date = new Date()): string {
  if (!isValidTimezone(tz) || tz === 'UTC' || tz === 'Etc/UTC') return 'UTC'
  const d = toDate(at)
  const p = zoneParts(d, tz)
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second)
  const minutes = Math.round((asUtc - Math.floor(d.getTime() / 1000) * 1000) / 60000)
  if (minutes === 0) return 'GMT'
  const h = Math.floor(Math.abs(minutes) / 60)
  const m = Math.abs(minutes) % 60
  return `GMT${minutes > 0 ? '+' : '-'}${h}${m ? `:${pad(m)}` : ''}`
}

export function formatDateTime(value: string | number | Date, b: Fmt, opts: { seconds?: boolean; zone?: boolean } = {}): string {
  const out = `${formatDate(value, b)} ${formatTime(value, b, opts.seconds)}`
  return opts.zone ? `${out} ${zoneLabel(b.timezone, value)}` : out
}

export function formatPeriod(from: string | Date, to: string | Date, b: Fmt): string {
  return `${formatDateTime(from, b)} — ${formatDateTime(to, b)} ${zoneLabel(b.timezone, to)}`
}

export function logoUrl(variant: 'light' | 'dark', version: string, fallback = true): string {
  const q = new URLSearchParams({ variant })
  if (!fallback) q.set('fallback', 'false')
  if (version) q.set('v', version)
  return `/api/v1/settings/company/logo?${q.toString()}`
}

/** Readable text colour (near-black or white) on a given brand colour. */
export function onColor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex)
  if (!m) return '#ffffff'
  const n = parseInt(m[1], 16)
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return lum > 0.45 ? '#0f172a' : '#ffffff'
}
