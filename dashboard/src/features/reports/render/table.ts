/**
 * Pure table helpers for the report renderer: column styles, sort keys that
 * understand units (ms, s, %, bps, bytes, durations, IPv4), search, CSV and
 * status / severity tones. Types-only imports so the unit tests can load it.
 */
import type { ColStyle, RenderTable } from '../types'

export type Cell = string | number | null | undefined
export type SortDir = 'asc' | 'desc'
export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export function cellText(v: Cell): string {
  if (v == null) return ''
  return typeof v === 'number' ? String(v) : v
}

const EMPTY = new Set(['', '—', '–', '-', 'n/a', 'na', 'none', 'null'])

export function isEmptyCell(v: Cell): boolean {
  return EMPTY.has(cellText(v).trim().toLowerCase())
}

const NUMERIC_CELL_RE = /^[<>≈~]?\s*-?[\d,]*\.?\d+\s*(%|ms|s|m|h|d|min|[kmgt]?bps|[kmgtp]?i?b|pps|x)?$/i

/** Column styles: the server's hints, else numeric when every non-empty cell is a measurement. */
export function inferStyles(table: Pick<RenderTable, 'headers' | 'rows' | 'styles'>): ColStyle[] {
  return table.headers.map((_, i) => {
    const hint = table.styles?.[i]
    if (hint) return hint
    if (i === 0) return 'text'
    const cells = table.rows.map((r) => cellText(r[i]).trim()).filter((c) => !EMPTY.has(c.toLowerCase()))
    return cells.length > 0 && cells.every((c) => NUMERIC_CELL_RE.test(c) || durationSeconds(c) != null) ? 'num' : 'text'
  })
}

export const isNumericStyle = (s: ColStyle | undefined) => s === 'num' || s === 'pct' || s === 'pct-bar'

/* ------------------------------------------------------------------ */
/*  Sort keys                                                           */
/* ------------------------------------------------------------------ */

const UNIT_SCALE: Record<string, number> = {
  '': 1, '%': 1, x: 1,
  ns: 1e-6, 'µs': 1e-3, us: 1e-3, ms: 1, s: 1000, sec: 1000, secs: 1000, min: 60_000, mins: 60_000,
  m: 60_000, h: 3_600_000, hr: 3_600_000, hrs: 3_600_000, d: 86_400_000, day: 86_400_000, days: 86_400_000,
  b: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3, tb: 1024 ** 4, pb: 1024 ** 5,
  kib: 1024, mib: 1024 ** 2, gib: 1024 ** 3, tib: 1024 ** 4,
  bps: 1, kbps: 1e3, mbps: 1e6, gbps: 1e9, tbps: 1e12,
  pps: 1, kpps: 1e3, mpps: 1e6,
  'b/s': 1, 'kb/s': 1024, 'mb/s': 1024 ** 2, 'gb/s': 1024 ** 3,
}

/** "3d 4h", "2h 5m", "45m 10s", "12s" → seconds; null when not a duration. */
export function durationSeconds(text: string): number | null {
  const m = /^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+(?:\.\d+)?)\s*s)?$/i.exec(text.trim())
  if (!m || !(m[1] || m[2] || m[3] || m[4])) return null
  // A bare "5m" / "12s" is a duration too; "5 ms" is not matched (ms handled as a unit).
  return (+(m[1] || 0)) * 86400 + (+(m[2] || 0)) * 3600 + (+(m[3] || 0)) * 60 + (+(m[4] || 0))
}

function ipv4(text: string): number | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d{1,2}))?$/.exec(text)
  if (!m) return null
  const o = m.slice(1, 5).map(Number)
  if (o.some((x) => x > 255)) return null
  return ((o[0] * 256 + o[1]) * 256 + o[2]) * 256 + o[3] + (m[5] ? +m[5] / 100 : 0)
}

export type SortKey = { n: number } | { s: string } | null

/** Comparable key: a number for measurements (units normalised), a string otherwise, null when empty. */
export function sortKey(v: Cell): SortKey {
  if (typeof v === 'number') return Number.isFinite(v) ? { n: v } : null
  const text = cellText(v).trim()
  if (EMPTY.has(text.toLowerCase())) return null
  const ip = ipv4(text)
  if (ip != null) return { n: ip }
  const m = /^([<>≈~]?)\s*(-?[\d,]*\.?\d+)\s*([a-zµ%/]*)$/i.exec(text)
  if (m && /\d/.test(m[2])) {
    const unit = m[3].toLowerCase()
    const scale = UNIT_SCALE[unit]
    if (scale !== undefined || /^[a-z]+$/i.test(m[3])) {
      const base = Number(m[2].replace(/,/g, '')) * (scale ?? 1)
      if (Number.isFinite(base)) {
        // "<0.1%" sorts just below 0.1%, ">99" just above 99.
        const nudge = m[1] === '<' ? -1e-9 : m[1] === '>' ? 1e-9 : 0
        return { n: base + nudge * Math.max(1, Math.abs(base)) }
      }
    }
  }
  const dur = durationSeconds(text)
  if (dur != null) return { n: dur * 1000 }
  return { s: text.toLowerCase() }
}

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })

/** Ascending comparison; numbers before text. Empty cells are handled by sortRows. */
export function compareKeys(a: NonNullable<SortKey>, b: NonNullable<SortKey>): number {
  if ('n' in a && 'n' in b) return a.n - b.n
  if ('n' in a) return -1
  if ('n' in b) return 1
  return collator.compare(a.s, b.s)
}

/** Stable sort by one column; empty cells always last, whatever the direction. */
export function sortRows<R extends Cell[]>(rows: R[], col: number, dir: SortDir): R[] {
  const keyed = rows.map((row, i) => ({ row, i, k: sortKey(row[col]) }))
  keyed.sort((a, b) => {
    if (a.k == null && b.k == null) return a.i - b.i
    if (a.k == null) return 1
    if (b.k == null) return -1
    const c = compareKeys(a.k, b.k)
    return c !== 0 ? (dir === 'asc' ? c : -c) : a.i - b.i
  })
  return keyed.map((x) => x.row)
}

/** Case-insensitive search: every whitespace-separated term must appear in some cell. */
export function filterRows<R extends Cell[]>(rows: R[], query: string): R[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return rows
  return rows.filter((row) => {
    const hay = row.map((c) => cellText(c).toLowerCase()).join('\u0001')
    return terms.every((t) => hay.includes(t))
  })
}

/** Default direction when a column is first sorted: largest first for measurements. */
export function firstSortDir(style: ColStyle | undefined): SortDir {
  return isNumericStyle(style) ? 'desc' : 'asc'
}

/* ------------------------------------------------------------------ */
/*  CSV                                                                 */
/* ------------------------------------------------------------------ */

function csvCell(v: Cell): string {
  let s = cellText(v)
  // Spreadsheet formula injection: neutralise cells that would be evaluated
  // (but keep plain negative numbers such as "-5", "-1.2 ms" or "-3 %" readable;
  // a unit is letters, µ, % or / only, so "-1+SUM(A1)" is still neutralised).
  if (/^[=+\-@\t\r]/.test(s) && !/^-[\d.,]+\s*[A-Za-zµ%/]*$/.test(s)) s = `'${s}`
  return /[",\r\n]/.test(s) || /^\s|\s$/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** RFC 4180 CSV (CRLF line endings) with a UTF-8 BOM so spreadsheets read non-ASCII names. */
export function toCsv(headers: string[], rows: Cell[][]): string {
  const lines = [headers.map(csvCell).join(','), ...rows.map((r) => headers.map((_, i) => csvCell(r[i])).join(','))]
  return `﻿${lines.join('\r\n')}\r\n`
}

/* ------------------------------------------------------------------ */
/*  Status, severity and percentage tones                              */
/* ------------------------------------------------------------------ */

const STATUS: Record<string, Tone> = {
  up: 'success', online: 'success', healthy: 'success', ok: 'success', success: 'success', succeeded: 'success',
  passed: 'success', pass: 'success', met: 'success', 'on track': 'success', completed: 'success',
  resolved: 'success', active: 'success', compliant: 'success', running: 'success', reachable: 'success',
  enabled: 'success', valid: 'success', good: 'success',
  down: 'danger', offline: 'danger', critical: 'danger', failed: 'danger', failure: 'danger', error: 'danger',
  breaching: 'danger', breached: 'danger', missed: 'danger', expired: 'danger', unreachable: 'danger',
  'non-compliant': 'danger', noncompliant: 'danger', stopped: 'danger', violated: 'danger',
  degraded: 'warning', 'at risk': 'warning', warning: 'warning', warn: 'warning', pending: 'warning',
  stale: 'warning', open: 'warning', expiring: 'warning', partial: 'warning', flapping: 'warning',
  changed: 'warning', drift: 'warning', unstable: 'warning',
  acknowledged: 'info', info: 'info', informational: 'info', maintenance: 'info', new: 'info', scheduled: 'info',
  suppressed: 'neutral', unknown: 'neutral', disabled: 'neutral', paused: 'neutral', 'no data': 'neutral',
  unmonitored: 'neutral', inactive: 'neutral', closed: 'neutral',
}

const SEVERITY: Record<string, Tone> = {
  critical: 'danger', emergency: 'danger', alert: 'danger', fatal: 'danger', high: 'danger', major: 'danger',
  error: 'danger', p1: 'danger', sev1: 'danger',
  medium: 'warning', warning: 'warning', warn: 'warning', minor: 'warning', moderate: 'warning', p2: 'warning', sev2: 'warning',
  low: 'info', info: 'info', informational: 'info', notice: 'info', p3: 'info', sev3: 'info',
  debug: 'neutral', none: 'neutral', unknown: 'neutral',
}

export function statusTone(value: Cell): Tone {
  const v = cellText(value).trim().toLowerCase()
  return STATUS[v] ?? SEVERITY[v] ?? 'neutral'
}

export function severityTone(value: Cell): Tone {
  const v = cellText(value).trim().toLowerCase()
  return SEVERITY[v] ?? STATUS[v] ?? 'neutral'
}

/** Leading number of a percentage cell ("99.95%", "<0.1%"); null when absent. */
export function pctValue(value: Cell): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  const m = /-?\d+(?:\.\d+)?/.exec(cellText(value).replace(/,/g, ''))
  return m ? Number(m[0]) : null
}

/**
 * Bar tone for a percentage column. Utilisation-like headers (used, CPU,
 * memory, disk, loss, errors) are bad when high; availability-like headers
 * (availability, uptime, SLA, budget, attainment, success) are good when high.
 */
export function pctTone(value: number | null, header: string): Tone | 'primary' {
  if (value == null) return 'neutral'
  const h = header.toLowerCase()
  if (/used|utili|cpu|mem|disk|load|loss|error|drop|saturat|full/.test(h)) {
    return value >= 90 ? 'danger' : value >= 75 ? 'warning' : 'primary'
  }
  if (/avail|uptime|sla|budget|attain|success|health|compliance|coverage/.test(h)) {
    return value >= 99 ? 'success' : value >= 90 ? 'warning' : 'danger'
  }
  return 'primary'
}
