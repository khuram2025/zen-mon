/**
 * Pure helpers for the report renderer: unit formatting, chart series
 * shaping, organisation-timezone axis labels, section status and layout.
 *
 * No React and no app imports (types only), so scripts/test-report-render.mjs
 * can load this file directly.
 */
import type { ChartSeries, RenderSection, SeriesPoint } from '../types'

/* ------------------------------------------------------------------ */
/*  Numbers and units                                                   */
/* ------------------------------------------------------------------ */

export type UnitKind = 'percent' | 'ms' | 'seconds' | 'bps' | 'Bps' | 'bits' | 'bytes' | 'pps' | 'count'

/** Classify a series unit. Case matters for bits (b) vs bytes (B). */
export function unitKind(unit?: string | null): UnitKind {
  const raw = (unit || '').trim()
  if (!raw) return 'count'
  if (raw === 'Bps' || raw === 'B/s' || raw === 'MBps' || raw === 'KBps') return 'Bps'
  if (raw === 'B') return 'bytes'
  const u = raw.toLowerCase()
  if (u === '%' || u === 'pct' || u === 'percent') return 'percent'
  if (u === 'ms' || u === 'msec' || u === 'millisecond' || u === 'milliseconds') return 'ms'
  if (u === 's' || u === 'sec' || u === 'secs' || u === 'second' || u === 'seconds') return 'seconds'
  if (u === 'bps' || u === 'bit/s' || u === 'bits/s' || u === 'b/s' || u === 'bits per second') return 'bps'
  if (u === 'bytes/s' || u === 'byte/s') return 'Bps'
  if (u === 'bit' || u === 'bits') return 'bits'
  if (u === 'byte' || u === 'bytes' || u === 'octets') return 'bytes'
  if (u === 'pps' || u === 'packets/s' || u === 'pkt/s') return 'pps'
  return 'count'
}

const GROUPED = new Map<number, Intl.NumberFormat>()

/** en-US grouping with at most `max` fraction digits (deterministic across browsers). */
export function fmtNumber(v: number, max = 0, min = 0): string {
  const key = max * 10 + min
  let f = GROUPED.get(key)
  if (!f) {
    f = new Intl.NumberFormat('en-US', { maximumFractionDigits: max, minimumFractionDigits: min })
    GROUPED.set(key, f)
  }
  return f.format(v)
}

/** Fraction digits that keep three significant figures for a scaled value. */
function sig(v: number, compact: boolean): number {
  const a = Math.abs(v)
  if (compact) return a >= 10 || Number.isInteger(v) ? 0 : 1
  return a >= 100 ? 0 : a >= 10 ? 1 : 2
}

function trim(s: string): string {
  return s.includes('.') ? s.replace(/\.?0+$/, '') : s
}

function scaled(v: number, base: number, units: string[], compact: boolean, sep = ' '): string {
  const a = Math.abs(v)
  let i = 0
  while (i < units.length - 1 && a >= base ** (i + 1)) i++
  const x = v / base ** i
  const s = x.toFixed(i === 0 && Number.isInteger(x) ? 0 : sig(x, compact))
  return `${compact ? trim(s) : s}${sep}${units[i]}`
}

function compactCount(v: number): string {
  const a = Math.abs(v)
  if (a < 1000) return trim(v.toFixed(Number.isInteger(v) ? 0 : a < 10 ? 2 : 1))
  const units = ['', 'k', 'M', 'B', 'T']
  let i = 0
  while (i < units.length - 1 && a >= 1000 ** (i + 1)) i++
  const x = v / 1000 ** i
  return `${trim(x.toFixed(Math.abs(x) >= 100 ? 0 : 1))}${units[i]}`
}

function duration(seconds: number, compact: boolean): string {
  const a = Math.abs(seconds)
  const sign = seconds < 0 ? '-' : ''
  if (a < 60) return `${sign}${trim(a.toFixed(a < 10 && !Number.isInteger(a) ? 1 : 0))} s`
  const d = Math.floor(a / 86400)
  const h = Math.floor((a % 86400) / 3600)
  const m = Math.floor((a % 3600) / 60)
  const s = Math.floor(a % 60)
  if (d > 0) return compact || !h ? `${sign}${d}d${h ? ` ${h}h` : ''}` : `${sign}${d}d ${h}h`
  if (h > 0) return `${sign}${h}h${m ? ` ${m}m` : ''}`
  return `${sign}${m}m${s && !compact ? ` ${s}s` : ''}`
}

export interface FormatOpts {
  /** Axis labels: shorter, fewer decimals. */
  compact?: boolean
}

/**
 * Format a measured value with its unit: `%` (two decimals; availability is
 * always two), `ms`, seconds, bits (bps, Kbps … SI), bytes (B, KB … binary),
 * packets and counts. `null` / non-finite values are "—" (never 0).
 */
export function formatValue(v: number | null | undefined, unit?: string | null, opts: FormatOpts = {}): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const compact = !!opts.compact
  const kind = unitKind(unit)
  switch (kind) {
    case 'percent':
      // Axis ticks keep their precision (zoomed availability axes tick at 99.25, 99.5 …).
      return compact ? `${trim(v.toFixed(2))}%` : `${v.toFixed(2)}%`
    case 'ms': {
      const a = Math.abs(v)
      if (a >= 1000) return `${trim((v / 1000).toFixed(compact ? 1 : 2))} s`
      if (a > 0 && a < 1) return `${compact ? trim(v.toFixed(2)) : v.toFixed(2)} ms`
      return `${trim(v.toFixed(compact || a >= 100 ? 0 : 1))} ms`
    }
    case 'seconds':
      return duration(v, compact)
    case 'bps':
      return scaled(v, 1000, ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'], compact)
    case 'Bps':
      return scaled(v, 1024, ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s'], compact)
    case 'bits':
      return scaled(v, 1000, ['b', 'Kb', 'Mb', 'Gb', 'Tb'], compact)
    case 'bytes':
      return scaled(v, 1024, ['B', 'KB', 'MB', 'GB', 'TB', 'PB'], compact)
    case 'pps':
      return `${compact ? compactCount(v) : fmtNumber(v, Math.abs(v) < 10 ? 2 : 0)} pps`
    default: {
      const label = (unit || '').trim()
      const n = compact ? compactCount(v) : fmtNumber(v, Number.isInteger(v) ? 0 : Math.abs(v) < 10 ? 2 : 1)
      return compact || !label ? n : `${n} ${label}`
    }
  }
}

/** Share of a total: "<0.1%" for a non-zero slice too small to show, never "0.0%". */
export function formatShare(value: number, total: number): string {
  if (!total || !Number.isFinite(value)) return '—'
  const pct = (value / total) * 100
  if (value > 0 && pct < 0.05) return '<0.1%'
  return `${pct.toFixed(1)}%`
}

/* ------------------------------------------------------------------ */
/*  Organisation-timezone wall clock (no app imports)                  */
/* ------------------------------------------------------------------ */

export interface ClockFmt {
  timezone: string
  date_format: string
  time_format: string
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WALL = new Map<string, Intl.DateTimeFormat>()

function wallFmt(tz: string): Intl.DateTimeFormat {
  let f = WALL.get(tz)
  if (!f) {
    try {
      f = new Intl.DateTimeFormat('en-US', {
        timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric',
        hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
      })
    } catch {
      f = wallFmt('UTC')
    }
    WALL.set(tz, f)
  }
  return f
}

export function wallClock(ms: number, tz: string): { y: number; mo: number; d: number; h: number; mi: number } {
  const p: Record<string, number> = {}
  for (const part of wallFmt(tz || 'UTC').formatToParts(new Date(ms))) {
    if (part.type !== 'literal') p[part.type] = Number(part.value)
  }
  return { y: p.year, mo: p.month, d: p.day, h: p.hour % 24, mi: p.minute }
}

const pad = (n: number) => String(n).padStart(2, '0')

/** Day and month in the organisation's date order, without the year ("07/10", "Oct 7"). */
export function shortDate(ms: number, b: ClockFmt): string {
  const w = wallClock(ms, b.timezone)
  switch (b.date_format) {
    case 'DD/MM/YYYY': return `${pad(w.d)}/${pad(w.mo)}`
    case 'MM/DD/YYYY': return `${pad(w.mo)}/${pad(w.d)}`
    case 'DD.MM.YYYY': return `${pad(w.d)}.${pad(w.mo)}`
    case 'DD-MMM-YYYY': return `${pad(w.d)}-${MONTHS[w.mo - 1]}`
    case 'MMM D, YYYY': return `${MONTHS[w.mo - 1]} ${w.d}`
    default: return `${pad(w.mo)}-${pad(w.d)}`
  }
}

export function shortTime(ms: number, b: ClockFmt): string {
  const w = wallClock(ms, b.timezone)
  if (b.time_format === '12h') return `${w.h % 12 || 12}:${pad(w.mi)} ${w.h < 12 ? 'AM' : 'PM'}`
  return `${pad(w.h)}:${pad(w.mi)}`
}

/**
 * Time-axis tick in the organisation timezone: times within a day or two,
 * dates for longer windows, and the date at midnight so day boundaries read.
 */
export function axisTimeLabel(ms: number, spanMs: number, b: ClockFmt): string {
  const DAY = 86_400_000
  if (spanMs > 3 * DAY) return shortDate(ms, b)
  const w = wallClock(ms, b.timezone)
  if (spanMs > DAY / 2 && w.h === 0 && w.mi === 0) return shortDate(ms, b)
  return shortTime(ms, b)
}

/* ------------------------------------------------------------------ */
/*  Chart series                                                        */
/* ------------------------------------------------------------------ */

/** Naive API timestamps are UTC (same rule as lib/branding). */
export function parseTime(t: string | null | undefined): number | null {
  if (!t) return null
  const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(t)
  const ms = Date.parse(naive ? `${t.replace(' ', 'T')}Z` : t)
  return Number.isFinite(ms) ? ms : null
}

function num(v: unknown): number | null {
  if (v == null || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

export interface TimeSeriesModel {
  kind: 'time'
  series: { name: string; color?: string; data: [number, number | null][] }[]
  start: number
  end: number
}

export interface CategoryModel {
  kind: 'category'
  labels: string[]
  series: { name: string; color?: string; values: (number | null)[] }[]
}

export type ChartModel = TimeSeriesModel | CategoryModel

function seriesList(s: ChartSeries, fallbackName: string): { name: string; color?: string; points: SeriesPoint[] }[] {
  if (s.series && s.series.length) {
    return s.series.map((x, i) => ({ name: x.name || `Series ${i + 1}`, color: x.color, points: x.points || [] }))
  }
  return [{ name: fallbackName, color: s.color, points: s.points || [] }]
}

/**
 * Shape a server series (single `points`, or multi-series `series`) for a
 * chart. Points with `t` make a time model (sorted, gaps kept as null);
 * points with `label` make a category model aligned by label. Returns null
 * when nothing was measured — a chart of no evidence is never drawn as zero.
 */
export function chartModel(s: ChartSeries, fallbackName = 'Value'): ChartModel | null {
  const list = seriesList(s, fallbackName)
  const timed = list.some((x) => x.points.some((p) => p.t != null))
  if (timed) {
    let start = Infinity
    let end = -Infinity
    let measured = false
    const series = list.map((x) => {
      const data: [number, number | null][] = []
      for (const p of x.points) {
        const ms = parseTime(p.t)
        if (ms == null) continue
        const v = num(p.v ?? p.value)
        if (v != null) measured = true
        data.push([ms, v])
        if (ms < start) start = ms
        if (ms > end) end = ms
      }
      data.sort((a, b) => a[0] - b[0])
      return { name: x.name, color: x.color, data }
    })
    return measured ? { kind: 'time', series, start, end } : null
  }
  const labels: string[] = []
  const seen = new Map<string, number>()
  for (const x of list) {
    for (const p of x.points) {
      const label = p.label ?? (p.t != null ? String(p.t) : null)
      if (label == null || seen.has(label)) continue
      seen.set(label, labels.length)
      labels.push(label)
    }
  }
  let measured = false
  const series = list.map((x) => {
    const values: (number | null)[] = labels.map(() => null)
    for (const p of x.points) {
      const i = p.label != null ? seen.get(p.label) : undefined
      if (i === undefined) continue
      const v = num(p.value ?? p.v)
      values[i] = v
      if (v != null) measured = true
    }
    return { name: x.name, color: x.color, values }
  })
  return measured && labels.length ? { kind: 'category', labels, series } : null
}

export interface Slice { label: string; value: number; share: string }

/** Donut / rank slices: positive values only, largest first for ranks. */
export function slices(points: SeriesPoint[] | undefined, sortDesc = false): Slice[] {
  const rows = (points || [])
    .map((p) => ({ label: String(p.label ?? p.t ?? ''), value: num(p.value ?? p.v) ?? 0 }))
    .filter((p) => p.label && p.value > 0)
  if (sortDesc) rows.sort((a, b) => b.value - a.value)
  const total = rows.reduce((a, r) => a + r.value, 0)
  return rows.map((r) => ({ ...r, share: formatShare(r.value, total) }))
}

/** One-sentence summary of a chart for screen readers. */
export function describeChart(title: string, model: ChartModel, unit: string | undefined,
  timeLabel: (ms: number) => string): string {
  const parts: string[] = []
  if (model.kind === 'time') {
    parts.push(`${title}: ${model.series.length} series from ${timeLabel(model.start)} to ${timeLabel(model.end)}.`)
    for (const s of model.series) {
      const vals = s.data.map((d) => d[1]).filter((v): v is number => v != null)
      if (!vals.length) {
        parts.push(`${s.name}: no data.`)
        continue
      }
      const last = [...s.data].reverse().find((d) => d[1] != null)?.[1] ?? null
      parts.push(`${s.name}: min ${formatValue(Math.min(...vals), unit)}, max ${formatValue(Math.max(...vals), unit)}, latest ${formatValue(last, unit)}.`)
    }
  } else {
    parts.push(`${title}: ${model.labels.length} categories.`)
    for (const s of model.series) {
      const pairs = model.labels
        .map((l, i) => [l, s.values[i]] as const)
        .filter((p) => p[1] != null)
        .slice(0, 5)
        .map(([l, v]) => `${l} ${formatValue(v, unit)}`)
      parts.push(`${model.series.length > 1 ? `${s.name}: ` : ''}${pairs.join(', ')}${model.labels.length > 5 ? ', …' : ''}.`)
    }
  }
  return parts.join(' ')
}

/* ------------------------------------------------------------------ */
/*  Sections                                                            */
/* ------------------------------------------------------------------ */

export type SectionStatus = 'ok' | 'warning' | 'danger' | 'error' | 'restricted'

/** Contents-rail status: failed or restricted sections, else the worst KPI accent. */
export function sectionStatus(s: Pick<RenderSection, 'kpis' | 'error' | 'restricted'>): SectionStatus {
  if (s.restricted) return 'restricted'
  if (s.error) return 'error'
  const accents = (s.kpis || []).map((k) => k.accent)
  if (accents.includes('danger')) return 'danger'
  if (accents.includes('warning')) return 'warning'
  return 'ok'
}

/** Stable, index-based anchor (a section may appear twice with different options). */
export function sectionAnchorId(prefix: string, index: number): string {
  return `${prefix}-s${index + 1}`
}

/** Pair consecutive half-width sections into two-column rows (indexes kept). */
export function layoutRows<T extends { half?: boolean }>(sections: T[]): { section: T; index: number }[][] {
  const rows: { section: T; index: number }[][] = []
  let i = 0
  while (i < sections.length) {
    if (sections[i].half && sections[i + 1]?.half) {
      rows.push([{ section: sections[i], index: i }, { section: sections[i + 1], index: i + 1 }])
      i += 2
    } else {
      rows.push([{ section: sections[i], index: i }])
      i += 1
    }
  }
  return rows
}

/** File-name safe slug for downloads. */
export function slug(s: string, fallback = 'report'): string {
  const out = (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60)
  return out || fallback
}
