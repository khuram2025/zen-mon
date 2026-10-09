// Pure logic behind the Changes page (Documentation/Inventory/03-UI-UX-SPEC.md section 3.7): the filter
// vocabulary of its address bar, the stats strip's windows and chips, the pairing of an installation and a
// removal into one upgrade row, grouping by calendar day, the daily chart's data and the footnotes. No React,
// no router, no API: scripts/test-inventory-os-hardware-changes.mjs exercises it directly.
//
// The URL carries the API's own parameter names (05-P1-API-CONTRACT.md section 3.7), so what the address bar
// says is what GET /inventory/changes receives. The numbers of the stats strip are the totals of the lists
// their chips open (section 4): a chip only sets category, action and label and keeps the window.

import { parseAssetAddress, type FilterCodec, type RejectedFilter } from '@/pages/inventory/filters'
import type { ChangesQuery } from '@/pages/inventory/api'
import type { ChangesFilters, ChangesWindowKey } from '@/pages/inventory/osHardwareChanges'
import type { ChangeAction, ChangeCategory, ChangeRow, ChangesResponse } from '@/pages/inventory/types'
import { changeAssetAddress, changeText, CATEGORY_LABELS } from './changeModel'
import { fmtInt, toMillis } from './model'

/* ── The filter vocabulary of the address bar ────────────────────────────── */

export const CHANGE_CATEGORIES: readonly ChangeCategory[] = ['software', 'os', 'firmware', 'hardware']
export const CHANGE_ACTIONS: readonly ChangeAction[] = ['added', 'removed', 'changed']
export const CHANGE_LABELS = ['upgrade', 'downgrade'] as const

/** Filter keys in the order they are written to a URL. */
export const CHANGE_FILTER_KEYS = ['q', 'kind', 'category', 'action', 'label', 'asset', 'subject_key', 'since', 'until'] as const
export type ChangeFilterKey = (typeof CHANGE_FILTER_KEYS)[number]

export const CHANGES_PATH = '/inventory/changes'
export const CHANGES_DEFAULT_PAGE_SIZE = 50

/** The API's limits (server/app/api/v1/inventory.py): `q` 200, `subject_key` 160. */
const MAX_TEXT = 200
const MAX_SUBJECT_KEY = 160
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/
// A full instant with a zone, the way the API writes them (`2026-10-01T12:00:00Z`, `…+02:00`, fractions allowed).
const INSTANT = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/i

type Parsed = { value: string } | { error: string }

/** The search box: the API reads it as words, so runs of white space collapse. */
const freeText = (raw: string): Parsed => {
  const t = raw.replace(/\s+/g, ' ').trim()
  if (!t) return { error: 'empty' }
  if (t.length > MAX_TEXT) return { error: `longer than ${MAX_TEXT} characters` }
  if (CONTROL.test(t)) return { error: 'contains control characters' }
  return { value: t }
}

/** An exact-match key: only the ends are trimmed (the API compares the stored string), and a value over its limit is reported. */
const exactText = (raw: string, max: number): Parsed => {
  const t = raw.trim()
  if (!t) return { error: 'empty' }
  if (t.length > max) return { error: `longer than ${max} characters` }
  if (CONTROL.test(t)) return { error: 'contains control characters' }
  return { value: t }
}

const oneOf = (allowed: readonly string[]) => (raw: string): Parsed => {
  const t = raw.trim().toLowerCase()
  return allowed.includes(t) ? { value: t } : { error: `expected one of ${allowed.join(', ')}` }
}

/** `2026-10-01T12:00:00.000+02:00` becomes `2026-10-01T10:00:00Z`: one canonical spelling, whole seconds. */
export function normaliseInstant(raw: string): string | null {
  const t = raw.trim()
  if (!INSTANT.test(t)) return null
  const ms = Date.parse(t.replace(' ', 'T').replace(/([+-]\d{2})(\d{2})$/, '$1:$2'))
  return Number.isFinite(ms) ? isoSeconds(ms) : null
}

/** Whole-second UTC ISO-8601 with a `Z`, the spelling every API timestamp uses. */
export const isoSeconds = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

const instant = (raw: string): Parsed => {
  const v = normaliseInstant(raw)
  return v ? { value: v } : { error: 'expected an ISO-8601 instant such as 2026-10-01T12:00:00Z' }
}

const address = (raw: string): Parsed => {
  const a = parseAssetAddress(raw)
  return a ? { value: `${a.kind}:${a.id}` } : { error: 'expected <kind>:<uuid>, for example device:3f2a9c10-7b1e-4d5a-9c0e-1a2b3c4d5e6f' }
}

const PARSERS: Record<ChangeFilterKey, (raw: string) => Parsed> = {
  q: freeText,
  kind: oneOf(['server', 'device']),
  category: oneOf(CHANGE_CATEGORIES),
  action: oneOf(CHANGE_ACTIONS),
  label: oneOf(CHANGE_LABELS),
  asset: address,
  subject_key: (raw) => exactText(raw, MAX_SUBJECT_KEY),
  since: instant,
  until: instant,
}

/**
 * Validates a filter object the way a query string is validated: a value outside its set, an unreadable
 * date or over-long text is dropped and reported, never passed on. Keys outside the vocabulary are ignored.
 * `until` must be after `since` (the API answers 422 otherwise).
 */
export function normaliseChangesFilters(input: ChangesFilters): { filters: ChangesFilters; rejected: RejectedFilter[] } {
  const filters: Record<string, string> = {}
  const rejected: RejectedFilter[] = []
  const source = input as Record<string, unknown>
  for (const key of CHANGE_FILTER_KEYS) {
    const raw = source[key]
    if (raw === undefined || raw === null || raw === '') continue
    const parsed = PARSERS[key](String(raw))
    if ('error' in parsed) rejected.push({ key, value: String(raw), reason: parsed.error })
    else filters[key] = parsed.value
  }
  if (filters.since && filters.until && Date.parse(filters.until) <= Date.parse(filters.since)) {
    rejected.push({ key: 'until', value: filters.until, reason: 'must be after since' })
    delete filters.until
  }
  return { filters: filters as ChangesFilters, rejected }
}

const asParams = (input: URLSearchParams | string): URLSearchParams =>
  typeof input === 'string' ? new URLSearchParams(input.replace(/^\?/, '')) : input

/** Reads the Changes filters out of a query string; for any key the first value wins. */
export function parseChangesFilters(input: URLSearchParams | string): { filters: ChangesFilters; rejected: RejectedFilter[] } {
  const params = asParams(input)
  const raw: Record<string, string> = {}
  for (const key of CHANGE_FILTER_KEYS) {
    const v = params.get(key)
    if (v !== null) raw[key] = v
  }
  return normaliseChangesFilters(raw as ChangesFilters)
}

/** Writes the vocabulary keys into `into`, replacing any it already holds. Values are validated like a read. */
export function writeChangesFilters(filters: ChangesFilters, into: URLSearchParams = new URLSearchParams()): URLSearchParams {
  for (const key of CHANGE_FILTER_KEYS) into.delete(key)
  const { filters: clean } = normaliseChangesFilters(filters)
  for (const key of CHANGE_FILTER_KEYS) {
    const v = clean[key]
    if (v !== undefined) into.set(key, v)
  }
  return into
}

export const isChangesFiltersEmpty = (f: ChangesFilters): boolean => CHANGE_FILTER_KEYS.every((k) => f[k] === undefined)

/** Lets one URL-state hook (useInventoryUrlState) serve the Changes page. */
export const changesCodec: FilterCodec<ChangesFilters> = {
  keys: CHANGE_FILTER_KEYS,
  parse: parseChangesFilters,
  write: writeChangesFilters,
  isEmpty: isChangesFiltersEmpty,
  count: (f) => CHANGE_FILTER_KEYS.filter((k) => f[k] !== undefined).length,
}

/** `/inventory/changes?...` for a filter set (and optionally a page): what a chip, a day or an Overview number links to. */
export function changesPageHref(filters: ChangesFilters, extra: { page?: number; page_size?: number } = {}): string {
  const p = writeChangesFilters(filters)
  if (extra.page && extra.page > 1) p.set('page', String(extra.page))
  if (extra.page_size && extra.page_size !== CHANGES_DEFAULT_PAGE_SIZE) p.set('page_size', String(extra.page_size))
  const qs = p.toString()
  return qs ? `${CHANGES_PATH}?${qs}` : CHANGES_PATH
}

/** What GET /inventory/changes receives for the page's filters: the same names, explicit paging, and the zone of the daily series. */
export function changesQuery(filters: ChangesFilters, page: number, pageSize: number, tz?: string): ChangesQuery {
  const { filters: f } = normaliseChangesFilters(filters)
  const q: ChangesQuery = {}
  if (f.category) q.category = f.category
  if (f.action) q.action = f.action
  if (f.kind) q.kind = f.kind
  if (f.asset) q.asset = f.asset
  if (f.subject_key) q.subject_key = f.subject_key
  if (f.q) q.q = f.q
  if (f.label) q.label = f.label
  if (f.since) q.since = f.since
  if (f.until) q.until = f.until
  if (tz) q.tz = tz
  q.page = page
  q.page_size = pageSize
  return q
}

/* ── Windows ─────────────────────────────────────────────────────────────── */

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export const CHANGE_WINDOWS: readonly { key: ChangesWindowKey; label: string; ms: number; words: string }[] = [
  { key: '24h', label: '24 h', ms: DAY, words: '24 hours' },
  { key: '7d', label: '7 d', ms: 7 * DAY, words: '7 days' },
  { key: '30d', label: '30 d', ms: 30 * DAY, words: '30 days' },
  { key: '90d', label: '90 d', ms: 90 * DAY, words: '90 days' },
]
/** The API's default window (the last seven days) is the page's default: it needs no `since`. */
export const DEFAULT_WINDOW: ChangesWindowKey = '7d'

/** `now - ms`, rounded down to the minute, as an API instant: a shared link carries a tidy time, not a stray second. */
export const windowSinceIso = (now: number, ms: number): string => isoSeconds(Math.floor((now - ms) / MINUTE) * MINUTE)

/** How far a window's length may differ from a preset's and still be called that preset (a bookmarked "7 d" opened later). */
const toleranceMs = (ms: number): number => Math.max(10 * MINUTE, Math.round(ms * 0.05))

/**
 * Which preset a `since`/`until` pair is. No `since` is the default window. A window with an `until` is a closed
 * range (a day clicked in the chart), never a preset. Otherwise the span from `since` to `now` is matched to a
 * preset within 5 percent, so the Overview's "7 days" link (`since` = its own `generated_at - 7 d`) reads as 7 d.
 */
export function windowOf(since: string | undefined, until: string | undefined, now: number): ChangesWindowKey | 'custom' {
  if (until) return 'custom'
  if (!since) return DEFAULT_WINDOW
  const at = toMillis(since)
  if (at === null) return 'custom'
  const span = now - at
  const hit = CHANGE_WINDOWS.find((w) => Math.abs(span - w.ms) <= toleranceMs(w.ms))
  return hit ? hit.key : 'custom'
}

/** The filters with a preset window applied: the default window has no `since`, any other writes one. */
export function withWindow(filters: ChangesFilters, key: ChangesWindowKey, now: number): ChangesFilters {
  const { since: _since, until: _until, ...rest } = filters
  if (key === DEFAULT_WINDOW) return rest
  const w = CHANGE_WINDOWS.find((x) => x.key === key) as (typeof CHANGE_WINDOWS)[number]
  return { ...rest, since: windowSinceIso(now, w.ms) }
}

/** The window of a clicked chart day: the local day in `tz`, from its first instant to the next day's (exclusive). */
export function withDay(filters: ChangesFilters, day: string, tz: string): ChangesFilters {
  const b = dayBounds(day, tz)
  return { ...filters, since: b.since, until: b.until }
}

/* ── Time zones (the daily series and the day groups are local days of one zone) ───────── */

const formatters = new Map<string, Intl.DateTimeFormat>()
function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
    formatters.set(tz, f)
  }
  return f
}

export function isValidTimeZone(tz: string | null | undefined): tz is string {
  if (!tz) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    return true
  } catch {
    return false
  }
}

/** The browser's IANA zone; `UTC` when it cannot say. Sent to the API so its daily series and the page's day groups agree. */
export function browserTimeZone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    return isValidTimeZone(tz) ? tz : 'UTC'
  } catch {
    return 'UTC'
  }
}

interface Wall {
  y: number
  m: number
  d: number
  h: number
  mi: number
  s: number
}

function wallClock(ms: number, tz: string): Wall {
  const parts: Record<string, string> = {}
  for (const p of formatter(tz).formatToParts(new Date(ms))) parts[p.type] = p.value
  return { y: +parts.year, m: +parts.month, d: +parts.day, h: +parts.hour % 24, mi: +parts.minute, s: +parts.second }
}

/** Offset of `tz` from UTC at an instant, in ms (east is positive). */
function offsetAt(ms: number, tz: string): number {
  const w = wallClock(ms, tz)
  return Date.UTC(w.y, w.m - 1, w.d, w.h, w.mi, w.s) - Math.floor(ms / 1000) * 1000
}

const pad = (n: number) => String(n).padStart(2, '0')

/** The local calendar day (`YYYY-MM-DD`) of an instant in `tz`. */
export function localDay(ms: number, tz: string): string {
  const w = wallClock(ms, tz)
  return `${w.y}-${pad(w.m)}-${pad(w.d)}`
}

/** The instant a local day begins in `tz` (settled across a DST change by looking at the offset twice). */
export function dayStart(day: string, tz: string): number {
  const [y, m, d] = day.split('-').map(Number)
  const wall = Date.UTC(y, m - 1, d)
  let t = wall - offsetAt(wall, tz)
  t = wall - offsetAt(t, tz)
  return t
}

const nextDay = (day: string): string => {
  const [y, m, d] = day.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10)
}

/** The API window of one local day: `since` inclusive, `until` exclusive (the next day's first instant). */
export function dayBounds(day: string, tz: string): { since: string; until: string } {
  return { since: isoSeconds(dayStart(day, tz)), until: isoSeconds(dayStart(nextDay(day), tz)) }
}

/** `14:05` in `tz`, 24-hour. */
export function timeLabel(ms: number, tz: string): string {
  const w = wallClock(ms, tz)
  return `${pad(w.h)}:${pad(w.mi)}`
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "Wed 7 Oct", with the year when it is not the current one. */
export function dayText(day: string, nowDay?: string): string {
  const [y, m, d] = day.split('-').map(Number)
  const weekday = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]
  const year = nowDay && nowDay.slice(0, 4) !== String(y) ? ` ${y}` : ''
  return `${weekday} ${d} ${MONTHS[m - 1]}${year}`
}

/** "Today", "Yesterday" or nothing: the relative word that goes in front of the date. */
export function relativeDay(day: string, nowDay: string): 'Today' | 'Yesterday' | null {
  if (day === nowDay) return 'Today'
  return nextDay(day) === nowDay ? 'Yesterday' : null
}

/** "1 Oct 12:00" in `tz`, with the year when it is not the current one. */
export function instantText(ms: number, tz: string, now: number): string {
  const day = localDay(ms, tz)
  return `${dayText(day, localDay(now, tz))} ${timeLabel(ms, tz)}`
}

/** Words for the window in force: a preset, or the explicit range. */
export function windowText(since: string | undefined, until: string | undefined, now: number, tz: string): string {
  const key = windowOf(since, until, now)
  if (key !== 'custom') return `last ${(CHANGE_WINDOWS.find((w) => w.key === key) as (typeof CHANGE_WINDOWS)[number]).words}`
  const from = toMillis(since)
  const to = toMillis(until)
  if (from !== null && to !== null) {
    // A whole local day is "Wed 7 Oct" rather than two instants.
    const day = localDay(from, tz)
    const b = dayBounds(day, tz)
    if (toMillis(b.since) === from && toMillis(b.until) === to) return dayText(day, localDay(now, tz))
    return `${instantText(from, tz, now)} to ${instantText(to, tz, now)}`
  }
  if (from !== null) return `since ${instantText(from, tz, now)}`
  return 'custom window'
}

/* ── Stats chips ─────────────────────────────────────────────────────────── */

export type ChipId = 'all' | 'installed' | 'removed' | 'upgraded' | 'downgraded' | 'os' | 'firmware' | 'hardware'

/** The part of the filters a chip owns; everything else (window, kind, asset, product, search) is kept. */
export type ChipFilters = Pick<ChangesFilters, 'category' | 'action' | 'label'>

export interface StatChip {
  id: ChipId
  label: string
  count: number
  filters: ChipFilters
  active: boolean
  /** What the number counts, for a tooltip and a screen reader. */
  hint: string
}

const CHIP_FILTERS: Record<ChipId, ChipFilters> = {
  all: {},
  installed: { category: 'software', action: 'added' },
  removed: { category: 'software', action: 'removed' },
  upgraded: { category: 'software', action: 'changed', label: 'upgrade' },
  downgraded: { category: 'software', action: 'changed', label: 'downgrade' },
  os: { category: 'os' },
  firmware: { category: 'firmware' },
  hardware: { category: 'hardware' },
}

/**
 * The chip a filter set stands on, or null when it is a combination no chip offers (the filter chips then say
 * it). `label` alone implies the software version-change rows, as the API reads it, so `?label=upgrade` is the
 * Upgraded chip.
 */
export function activeChipId(f: Pick<ChangesFilters, 'category' | 'action' | 'label'>): ChipId | null {
  const { category, action, label } = f
  if (!category && !action && !label) return 'all'
  if (label) {
    if (category && category !== 'software') return null
    if (action && action !== 'changed') return null
    return label === 'upgrade' ? 'upgraded' : 'downgraded'
  }
  if (category === 'software' && action === 'added') return 'installed'
  if (category === 'software' && action === 'removed') return 'removed'
  if (!action && (category === 'os' || category === 'firmware' || category === 'hardware')) return category
  return null
}

/** The eight chips, in the order of the strip, with the window's numbers from `stats`. */
export function statChips(stats: ChangesResponse['stats'], f: ChangesFilters): StatChip[] {
  const sw = stats.software
  const current = activeChipId(f)
  const make = (id: ChipId, label: string, count: number, hint: string): StatChip => ({ id, label, count, filters: CHIP_FILTERS[id], active: current === id, hint })
  return [
    make('all', 'All changes', stats.total, 'Every change in the window'),
    make('installed', 'Installed', sw.added, 'Software installations'),
    make('removed', 'Removed', sw.removed, 'Software removals'),
    make('upgraded', 'Upgraded', sw.upgraded, 'Software upgrades the server could order by version'),
    make('downgraded', 'Downgraded', sw.downgraded, 'Software downgrades the server could order by version'),
    make('os', 'OS changes', stats.os, 'Operating system changes'),
    make('firmware', 'Firmware changes', stats.firmware, 'Network firmware changes'),
    make('hardware', 'Hardware changes', stats.hardware, 'Hardware model and serial number changes'),
  ]
}

/** The filters after clicking a chip: the chip's category, action and label replace the old ones; the rest stays. */
export function applyChip(f: ChangesFilters, id: ChipId): ChangesFilters {
  const { category: _c, action: _a, label: _l, ...rest } = f
  return { ...rest, ...CHIP_FILTERS[id] }
}

/**
 * The informational line under the strip: upgrades and downgrades recorded as an installation plus a removal
 * are not in the Upgraded and Downgraded numbers (those count `changed` rows, so they equal the lists they
 * open); this says how many there are, as text.
 */
export function pairedNote(stats: ChangesResponse['stats']): string | null {
  const { upgraded, downgraded } = stats.software.paired
  if (upgraded + downgraded <= 0) return null
  const parts: string[] = []
  if (upgraded > 0) parts.push(`${fmtInt(upgraded)} ${upgraded === 1 ? 'upgrade' : 'upgrades'}`)
  if (downgraded > 0) parts.push(`${fmtInt(downgraded)} ${downgraded === 1 ? 'downgrade' : 'downgrades'}`)
  return `Plus ${parts.join(' and ')} recorded as an installation and a removal. The list shows each pair as one row.`
}

/* ── Active filters (the chips under the toolbar) ─────────────────────────── */

export interface ActiveFilter {
  id: string
  label: string
  text: string
  /** The filters with this one removed. */
  without: ChangesFilters
}

const without = (f: ChangesFilters, ...keys: ChangeFilterKey[]): ChangesFilters => {
  const next: Record<string, unknown> = { ...f }
  for (const k of keys) delete next[k]
  return next as ChangesFilters
}

/**
 * What narrows the list besides the stats chips, the search box and the kind switch: an asset, a product, a window
 * that is not a preset, and any category/action/label combination no stats chip stands for. Each can be
 * removed on its own. `names` carries the display names the page has learnt (an asset's name, a product's).
 */
export function activeFilters(f: ChangesFilters, now: number, tz: string, names: { asset?: string | null; subject?: string | null } = {}): ActiveFilter[] {
  const out: ActiveFilter[] = []
  if (f.asset) out.push({ id: 'asset', label: 'Asset', text: names.asset || f.asset, without: without(f, 'asset') })
  if (f.subject_key) out.push({ id: 'subject_key', label: 'Product', text: names.subject || f.subject_key, without: without(f, 'subject_key') })
  if (activeChipId(f) === null) {
    if (f.category) out.push({ id: 'category', label: 'Category', text: CATEGORY_LABELS[f.category], without: without(f, 'category') })
    if (f.action) out.push({ id: 'action', label: 'Action', text: ACTION_TEXT[f.action], without: without(f, 'action') })
    if (f.label) out.push({ id: 'label', label: 'Version change', text: f.label === 'upgrade' ? 'Upgrades' : 'Downgrades', without: without(f, 'label') })
  }
  if (windowOf(f.since, f.until, now) === 'custom') {
    out.push({ id: 'window', label: 'Window', text: windowText(f.since, f.until, now, tz), without: without(f, 'since', 'until') })
  }
  return out
}

const ACTION_TEXT: Record<ChangeAction, string> = { added: 'Added', removed: 'Removed', changed: 'Changed' }

/* ── Pairing and entries ─────────────────────────────────────────────────── */

export type Glyph = 'installed' | 'removed' | 'upgraded' | 'downgraded' | 'changed' | 'os' | 'firmware' | 'hardware' | 'marker'

/** One row of the event list: an event, an installation + removal pair shown as one upgrade, or a budget marker. */
export interface ChangeEntry {
  key: string
  kind: 'event' | 'pair' | 'marker'
  /** The instant of the event (both halves of a pair share it). */
  at: string
  category: ChangeCategory
  glyph: Glyph
  assetName: string | null
  assetPresent: boolean
  /** `<kind>:<uuid>` of the asset whose flyout the name opens; null when it has been deleted. */
  address: string | null
  /** What changed ("Google Chrome"). */
  subject: string
  /** What happened ("upgraded", "installed"). */
  verb: string
  /** The values ("151.0.7922.110 → 152.0.8001.12"). */
  detail: string | null
  /** A pair whose two halves carry different titles ("Java 8 Update 391"): the title it replaced. */
  replaces: string | null
  /** An unmerged half of a pair whose other half is on another page. */
  partnerElsewhere: boolean
  /** Recorded from a software list that was cut at the agent's cap. */
  truncated: boolean
  sentence: string
  /** The events behind the row (two for a pair). */
  ids: number[]
}

const eventGlyph = (r: Pick<ChangeRow, 'category' | 'action' | 'label' | 'marker'>): Glyph => {
  if (r.marker) return 'marker'
  if (r.category !== 'software') return r.category
  if (r.action === 'added') return 'installed'
  if (r.action === 'removed') return 'removed'
  return r.label === 'upgrade' ? 'upgraded' : r.label === 'downgrade' ? 'downgraded' : 'changed'
}

const isPairCandidate = (r: ChangeRow): boolean =>
  !r.marker && r.category === 'software' && !!r.subject_key && (r.action === 'added' || r.action === 'removed')

const pairKeyOf = (r: ChangeRow): string => `${r.asset_key}|${r.subject_key}|${toMillis(r.observed_at) ?? r.observed_at}`

/**
 * Pairing rule (doc 03 section 3.7): a product whose title embeds its version (Java, 7-Zip, Visual C++
 * redistributables) is stored as a new row, so an upgrade is one `removed` plus one `added` event written in
 * the same transaction. Exactly one of each for the same `subject_key` on the same asset at the same
 * `observed_at` is shown as ONE row. Events of different assets, products or instants never pair, and neither
 * do two installations or two removals (the API does not pair those either). The label (upgrade or downgrade)
 * is the server's own, from the version comparator; with none the row says "changed", never a guess.
 */
export function pairEvents(rows: readonly ChangeRow[]): { pairs: Map<number, ChangeRow>; label: Map<number, 'upgrade' | 'downgrade' | null> } {
  const groups = new Map<string, { added: ChangeRow[]; removed: ChangeRow[] }>()
  for (const r of rows) {
    if (!isPairCandidate(r)) continue
    const key = pairKeyOf(r)
    const g = groups.get(key) ?? { added: [], removed: [] }
    groups.set(key, g)
    g[r.action === 'added' ? 'added' : 'removed'].push(r)
  }
  const pairs = new Map<number, ChangeRow>()
  const label = new Map<number, 'upgrade' | 'downgrade' | null>()
  for (const g of groups.values()) {
    if (g.added.length !== 1 || g.removed.length !== 1) continue
    const [a] = g.added
    const [r] = g.removed
    pairs.set(a.id, r)
    pairs.set(r.id, a)
    // Only a label the server attached to this very pair counts; two that disagree are no label.
    const fromA = a.pair && a.pair.event_id === r.id ? a.pair.label : undefined
    const fromR = r.pair && r.pair.event_id === a.id ? r.pair.label : undefined
    const l = fromA !== undefined && fromR !== undefined ? (fromA === fromR ? fromA : null) : (fromA ?? fromR ?? null)
    label.set(a.id, l)
    label.set(r.id, l)
  }
  return { pairs, label }
}

/** The note for an event of a pair whose other half is not on this page. */
export const PARTNER_ELSEWHERE_NOTE = 'The other half of this change is on another page.'

/** The cap the agent puts on a software list, said in the note (05-P1-API-CONTRACT.md `snapshot_truncated`). */
export const SNAPSHOT_CAP = 1000

export function truncatedNote(entry: Pick<ChangeEntry, 'verb' | 'kind'>): string {
  const cap = `The list was capped at ${fmtInt(SNAPSHOT_CAP)} titles when this was recorded`
  return entry.verb === 'removed' && entry.kind === 'event'
    ? `${cap}, so this title may have been cut off rather than removed.`
    : `${cap}, so the picture may be incomplete.`
}

const versionChange = (oldV: string | null, newV: string | null): string | null => {
  const o = (oldV ?? '').trim()
  const n = (newV ?? '').trim()
  return o && n ? `${o} → ${n}` : n ? `to ${n}` : o ? `from ${o}` : null
}

/** Rows of one page as list entries, newest first as given: pairs merged into one row at the place of their first half. */
export function buildEntries(rows: readonly ChangeRow[]): { entries: ChangeEntry[]; merged: number } {
  const { pairs, label } = pairEvents(rows)
  const onPage = new Set(rows.map((r) => r.id))
  const done = new Set<number>()
  const entries: ChangeEntry[] = []
  let merged = 0
  for (const r of rows) {
    if (done.has(r.id)) continue
    const partner = pairs.get(r.id)
    const base = {
      at: r.observed_at,
      assetName: r.asset_name,
      assetPresent: r.asset_present,
      address: changeAssetAddress(r),
    }
    if (partner) {
      const added = r.action === 'added' ? r : partner
      const removed = r.action === 'removed' ? r : partner
      const l = label.get(r.id) ?? null
      const verb = l === 'upgrade' ? 'upgraded' : l === 'downgrade' ? 'downgraded' : 'changed'
      const detail = versionChange(removed.old_value, added.new_value)
      const replaces = removed.subject !== added.subject ? removed.subject : null
      done.add(r.id)
      done.add(partner.id)
      merged += 1
      entries.push({
        ...base,
        key: `pair:${removed.id}:${added.id}`,
        kind: 'pair',
        category: 'software',
        glyph: l === 'upgrade' ? 'upgraded' : l === 'downgrade' ? 'downgraded' : 'changed',
        subject: added.subject,
        verb,
        detail,
        replaces,
        partnerElsewhere: false,
        truncated: added.snapshot_truncated || removed.snapshot_truncated,
        sentence: `${added.subject} ${verb}${detail ? ` ${detail}` : ''}${replaces ? ` (replaces ${replaces})` : ''}`,
        ids: [removed.id, added.id],
      })
      continue
    }
    const t = changeText(r)
    done.add(r.id)
    entries.push({
      ...base,
      key: `event:${r.id}`,
      kind: r.marker ? 'marker' : 'event',
      category: r.category,
      glyph: eventGlyph(r),
      subject: t.subject,
      verb: t.verb,
      detail: t.detail,
      replaces: null,
      partnerElsewhere: !!r.pair && !onPage.has(r.pair.event_id),
      truncated: r.snapshot_truncated,
      sentence: t.sentence,
      ids: [r.id],
    })
  }
  return { entries, merged }
}

/* ── Day groups ──────────────────────────────────────────────────────────── */

export interface DayGroup {
  /** `YYYY-MM-DD` in the zone the groups were made for. */
  day: string
  /** "Wed 7 Oct". */
  text: string
  relative: 'Today' | 'Yesterday' | null
  entries: ChangeEntry[]
}

/** Entries (newest first) grouped by local calendar day in `tz`, keeping their order. */
export function groupByDay(entries: readonly ChangeEntry[], tz: string, now: number): DayGroup[] {
  const nowDay = localDay(now, tz)
  const groups: DayGroup[] = []
  for (const e of entries) {
    const at = toMillis(e.at)
    const day = at === null ? 'unknown' : localDay(at, tz)
    const last = groups[groups.length - 1]
    if (last && last.day === day) last.entries.push(e)
    else groups.push({ day, text: day === 'unknown' ? 'Unknown date' : dayText(day, nowDay), relative: day === 'unknown' ? null : relativeDay(day, nowDay), entries: [e] })
  }
  return groups
}

/** "14:05" of an entry in `tz`. */
export function entryTime(e: Pick<ChangeEntry, 'at'>, tz: string): string {
  const at = toMillis(e.at)
  return at === null ? '--:--' : timeLabel(at, tz)
}

/* ── The daily chart ─────────────────────────────────────────────────────── */

export const CHART_SERIES: readonly { key: ChangeCategory; label: string; color: number }[] = [
  { key: 'software', label: 'Software', color: 0 },
  { key: 'os', label: 'OS', color: 1 },
  { key: 'firmware', label: 'Firmware', color: 2 },
  { key: 'hardware', label: 'Hardware', color: 3 },
]

export interface ChartDay {
  day: string
  /** Axis label, "7 Oct"; a partial day carries an asterisk ("2 Oct*"). */
  label: string
  /** Full label, "Wed 7 Oct". */
  full: string
  software: number
  os: number
  firmware: number
  hardware: number
  total: number
  /**
   * Why the bar covers less than a whole local day (review finding UI-20): the window starts part-way through the
   * first day, or the last day is today and is not over. Null for a whole day.
   */
  partial: null | 'start' | 'today'
  /** "from 09:33" / "so far today": the words for a tooltip and the table. */
  partialText: string | null
}

/**
 * The daily series as chart days. A "last 7 days" window that starts at 09:33 spans EIGHT calendar days: the first
 * has only the changes from 09:33 and today has only those so far, so both bars can read as quiet days. They are
 * marked partial (`since` is the response's `window.since`).
 */
export function chartDays(series: ChangesResponse['series'], now: number, tz: string, since?: string | null): ChartDay[] {
  const nowDay = localDay(now, tz)
  const from = toMillis(since)
  return series.map((p, i) => {
    const [, m, d] = p.day.split('-').map(Number)
    let partial: ChartDay['partial'] = null
    let partialText: string | null = null
    if (i === 0 && from !== null && from > dayStart(p.day, tz) + 60_000) {
      partial = 'start'
      partialText = `from ${timeLabel(from, tz)}`
    } else if (p.day === nowDay) {
      partial = 'today'
      partialText = 'so far today'
    }
    return { ...p, label: `${d} ${MONTHS[m - 1]}${partial ? '*' : ''}`, full: dayText(p.day, nowDay), partial, partialText }
  })
}

/** Events per category over the series: what the legend shows. */
export function seriesTotals(series: ChangesResponse['series']): Record<ChangeCategory | 'total', number> {
  const t = { software: 0, os: 0, firmware: 0, hardware: 0, total: 0 }
  for (const p of series) {
    t.software += p.software
    t.os += p.os
    t.firmware += p.firmware
    t.hardware += p.hardware
    t.total += p.total
  }
  return t
}

/** A chart of this many days or fewer labels every bar; longer ones let the axis thin its labels. */
export const CHART_DENSE_DAYS = 31

/** One sentence for the chart's accessible name: "Changes per day, 8 days, 56 in total, busiest Wed 7 Oct with 18". */
export function chartSummary(days: readonly ChartDay[]): string {
  if (days.length === 0) return 'Changes per day: no days in the window'
  const total = days.reduce((n, d) => n + d.total, 0)
  const busiest = days.reduce((a, b) => (b.total > a.total ? b : a))
  const partial = days.filter((d) => d.partial).length
  const calendar = partial > 0 ? `${days.length} calendar ${days.length === 1 ? 'day' : 'days'}, ${partial === 1 ? 'one of them partial' : 'the first and last partial'}` : `${days.length} ${days.length === 1 ? 'day' : 'days'}`
  return `Changes per day, ${calendar}, ${fmtInt(total)} in total${busiest.total > 0 ? `, busiest ${busiest.full} with ${fmtInt(busiest.total)}` : ''}`
}

/* ── The footnote ────────────────────────────────────────────────────────── */

/** What the Changes response says about how far back its history reaches. */
export type HistoryInfo = Pick<ChangesResponse, 'history_start' | 'retention_days'>

/**
 * The data-integrity footnote (review findings FE-08, UP-08 and DT-04). Two facts, both from the API and neither
 * guessed from the events on the page: when change capture BEGAN (`history_start`; a quiet estate may have recorded
 * its first change days later, and a scoped user's first visible change is later still) and the retention period
 * after which events are removed. Older than the retention window means the history now starts later than the
 * capture did, and the sentence says so, so a purged change is never described as "not recorded".
 * While the response is not there (or the start is not known) no date is named.
 */
export function historyText(info: HistoryInfo | null | undefined, now: number, tz: string): { text: string; title: string | null } {
  const days = info?.retention_days
  const removal = typeof days === 'number' && days > 0 ? `Changes older than ${fmtInt(days)} ${days === 1 ? 'day' : 'days'} are removed.` : 'Changes older than the retention period are removed.'
  const at = toMillis(info?.history_start)
  if (!info || at === null) {
    return { text: `History starts when change capture was enabled. ${removal}`, title: null }
  }
  const startDay = localDay(at, tz)
  const full = (day: string) => `${dayText(day)} ${day.slice(0, 4)}`
  const keptFrom = typeof days === 'number' && days > 0 ? now - days * DAY : null
  if (keptFrom !== null && keptFrom > at) {
    const keptDay = localDay(keptFrom, tz)
    return {
      text: `Change capture began ${full(startDay)}. ${removal} The history you can see therefore starts ${full(keptDay)}.`,
      title: `Change capture began ${instantText(at, tz, now)} (${tz})`,
    }
  }
  return {
    text: `History starts ${full(startDay)}, when change capture began. ${removal}`,
    title: `Change capture began ${instantText(at, tz, now)} (${tz})`,
  }
}

/** How many events a page shows against how many match: "56 events in 50 rows". */
export function countText(total: number, rows: number, merged: number): string {
  const events = `${fmtInt(total)} ${total === 1 ? 'event' : 'events'}`
  return merged > 0 ? `${events}, ${fmtInt(rows)} ${rows === 1 ? 'row' : 'rows'} on this page (${fmtInt(merged)} ${merged === 1 ? 'pair' : 'pairs'} of installation and removal shown as one upgrade)` : events
}

/** Whether the page asked for is past the last one (the API answers 200 with no items). */
export function pastTheEnd(page: number, total: number, pageSize: number): { beyond: boolean; pageCount: number } {
  const pageCount = Math.max(1, Math.ceil(total / Math.max(1, pageSize)))
  return { beyond: page > pageCount && total > 0, pageCount }
}
