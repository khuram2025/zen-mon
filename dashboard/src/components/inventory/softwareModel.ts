// Pure logic behind the Software catalog (Documentation/Inventory/03-UI-UX-SPEC.md section 3.4,
// 05-P1-API-CONTRACT.md sections 3.8, 3.9 and 4): the URL vocabulary, sort rules, the version bar, the
// footer and the drill links. No React, no router, no API: scripts/test-inventory-software-settings.mjs
// exercises it directly and the page only renders what it returns.
//
// Principle 1: every number is a link to the list behind it and the drilled list's total equals the
// number clicked. The numbers with no list (distribution.other, hidden_installs) stay text.

import {
  assetsHref, exactText, freeText, parseListState, type FilterCodec, type RejectedFilter, type SortOrder,
} from '@/pages/inventory/filters'
import type { CatalogResponse, CatalogRow, NormStatus, PublishersResponse, VersionBar } from '@/pages/inventory/softwareSettings'
import { fmtInt, plural } from './model'

export const SOFTWARE_PATH = '/inventory/software'

/** `/inventory/software/<product key>`, optionally on a tab. */
export function productPath(productKey: string, extra: Record<string, string | undefined> = {}): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v)
  const qs = p.toString()
  return `${SOFTWARE_PATH}/${encodeURIComponent(productKey)}${qs ? `?${qs}` : ''}`
}

/* ── Vocabulary ──────────────────────────────────────────────────────────── */

/** The product categories the API accepts (contract 3.8); anything else is a 422. */
export const SOFTWARE_CATEGORIES = [
  'browser', 'security', 'endpoint-protection', 'remote-access', 'database', 'web-server', 'runtime', 'development',
  'productivity', 'monitoring', 'backup', 'virtualization', 'networking', 'driver', 'os-component', 'update', 'other',
] as const

const CATEGORY_WORDS: Record<string, string> = {
  'endpoint-protection': 'Endpoint protection',
  'remote-access': 'Remote access',
  'web-server': 'Web server',
  'os-component': 'OS component',
}

/** "Remote access", "Browser": the words for a category key; an unknown key is shown tidied, not dropped. */
export function categoryLabel(category: string | null | undefined): string {
  const key = (category ?? '').trim().toLowerCase()
  if (!key) return 'Other'
  if (CATEGORY_WORDS[key]) return CATEGORY_WORDS[key]
  const words = key.replace(/[-_]+/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export const NORM_STATUS_META: Record<NormStatus, { label: string; tone: string; meaning: string }> = {
  normalized: {
    label: 'Normalized',
    tone: 'border-success/30 bg-success/10 text-success-fg',
    meaning: 'A catalog rule placed this product',
  },
  publisher_only: {
    label: 'Publisher only',
    tone: 'border-warning/35 bg-warning/10 text-warning-fg',
    meaning: 'No product rule matched; only the publisher is recognised',
  },
  unmatched: {
    label: 'Unmatched',
    tone: 'border-danger/30 bg-danger/10 text-danger-fg',
    meaning: 'Neither the product nor the publisher is recognised',
  },
}

export function normStatusMeta(status: string | null | undefined) {
  return NORM_STATUS_META[(status ?? '') as NormStatus] ?? NORM_STATUS_META.unmatched
}

/** `unknown` is the value of the publisher filter for products with no publisher (contract 3.9). */
export const UNKNOWN_PUBLISHER = 'unknown'

/* ── Filters ─────────────────────────────────────────────────────────────── */

/** What GET /inventory/software accepts besides sort and paging. The URL carries the same names. */
export interface CatalogFilters {
  q?: string
  /** Repeatable; a category named here is never hidden by `hidden=exclude`. Kept sorted: it is a set. */
  category?: string[]
  /** A `value` of /software/publishers (`unknown`: no publisher). */
  publisher?: string
  /** `include` shows the default-hidden categories too; `exclude` (the default) is never written. */
  hidden?: 'include'
  /** Only products the normaliser could not place. */
  unmatched?: true
}

export const CATALOG_FILTER_KEYS = ['q', 'category', 'publisher', 'hidden', 'unmatched'] as const
export type CatalogFilterKey = (typeof CATALOG_FILTER_KEYS)[number]

export const CATALOG_SORT_KEYS = ['name', 'publisher', 'category', 'hosts', 'version_count', 'latest_version', 'first_seen'] as const
export type CatalogSort = (typeof CATALOG_SORT_KEYS)[number]

export interface CatalogListState {
  sort?: CatalogSort
  order?: SortOrder
  page?: number
  page_size?: number
}
export type CatalogQuery = CatalogFilters & CatalogListState

export const CATALOG_DEFAULT_SORT: { sort: CatalogSort; order: SortOrder } = { sort: 'hosts', order: 'desc' }
export const CATALOG_DEFAULT_PAGE_SIZE = 50

/** The API's own default direction: counts newest/biggest first, everything else A to Z. */
export function defaultOrderFor(sort: string): SortOrder {
  return sort === 'hosts' || sort === 'version_count' ? 'desc' : 'asc'
}

/** The API's own limits (server/app/api/v1/inventory.py): a longer value is reported, never sent to be refused with a 422. */
const MAX_TEXT = 200
const MAX_PUBLISHER = 96
const MAX_CATEGORIES = 16

/**
 * The search text is words (runs of white space collapse); the publisher is an EXACT value, so only its ends are
 * trimmed (FE-04, the rule filters.ts applies to every exact-match value).
 */
const cleanText = (raw: string, max = MAX_TEXT) => freeText(raw, max)
const cleanExact = (raw: string, max = MAX_TEXT) => exactText(raw, max)

/** Validates a filter object like a query string: unknown categories and bad text are dropped and reported. */
export function normaliseCatalogFilters(input: CatalogFilters & { hidden?: string; unmatched?: boolean | string }): { filters: CatalogFilters; rejected: RejectedFilter[] } {
  const filters: CatalogFilters = {}
  const rejected: RejectedFilter[] = []
  // Blank text is no filter, not an error.
  if (input.q !== undefined && input.q !== null && String(input.q).trim() !== '') {
    const t = cleanText(String(input.q))
    if ('error' in t) rejected.push({ key: 'q', value: String(input.q), reason: t.error })
    else filters.q = t.value
  }
  const rawCategories = Array.isArray(input.category) ? input.category : input.category ? [String(input.category)] : []
  const categories = new Set<string>()
  for (const raw of rawCategories) {
    const c = String(raw).trim().toLowerCase()
    if (!c) continue
    if (!(SOFTWARE_CATEGORIES as readonly string[]).includes(c)) {
      rejected.push({ key: 'category', value: String(raw), reason: 'not a software category' })
      continue
    }
    if (categories.size < MAX_CATEGORIES) categories.add(c)
  }
  if (categories.size) filters.category = [...categories].sort()
  if (input.publisher !== undefined && input.publisher !== null && String(input.publisher).trim() !== '') {
    const t = cleanExact(String(input.publisher), MAX_PUBLISHER)
    if ('error' in t) rejected.push({ key: 'publisher', value: String(input.publisher), reason: t.error })
    else filters.publisher = t.value
  }
  const hidden = input.hidden === undefined || input.hidden === null ? '' : String(input.hidden).trim().toLowerCase()
  if (hidden === 'include') filters.hidden = 'include'
  else if (hidden && hidden !== 'exclude') rejected.push({ key: 'hidden', value: String(input.hidden), reason: 'expected include or exclude' })
  const um = input.unmatched === undefined || input.unmatched === null ? '' : String(input.unmatched).trim().toLowerCase()
  if (um === 'true' || um === '1') filters.unmatched = true
  else if (um && um !== 'false' && um !== '0') rejected.push({ key: 'unmatched', value: String(input.unmatched), reason: 'expected true or false' })
  return { filters, rejected }
}

export function parseCatalogFilters(input: URLSearchParams | string): { filters: CatalogFilters; rejected: RejectedFilter[] } {
  const params = typeof input === 'string' ? new URLSearchParams(input.replace(/^\?/, '')) : input
  const raw: Record<string, unknown> = {}
  const categories = params.getAll('category')
  if (categories.length) raw.category = categories
  for (const key of ['q', 'publisher', 'hidden', 'unmatched'] as const) {
    const v = params.get(key)
    if (v !== null) raw[key] = v
  }
  return normaliseCatalogFilters(raw as CatalogFilters)
}

/** Writes the vocabulary keys into `into`, replacing any it holds. The defaults (`hidden=exclude`) are not written. */
export function writeCatalogFilters(filters: CatalogFilters, into: URLSearchParams = new URLSearchParams()): URLSearchParams {
  for (const key of CATALOG_FILTER_KEYS) into.delete(key)
  const { filters: clean } = normaliseCatalogFilters(filters)
  if (clean.q) into.set('q', clean.q)
  for (const c of clean.category ?? []) into.append('category', c)
  if (clean.publisher) into.set('publisher', clean.publisher)
  if (clean.hidden) into.set('hidden', 'include')
  if (clean.unmatched) into.set('unmatched', 'true')
  return into
}

export const isCatalogFiltersEmpty = (f: CatalogFilters): boolean => CATALOG_FILTER_KEYS.every((k) => k === 'category' ? !f.category?.length : f[k] === undefined)

export const catalogFilterCodec: FilterCodec<CatalogFilters> = {
  keys: CATALOG_FILTER_KEYS,
  parse: parseCatalogFilters,
  write: writeCatalogFilters,
  isEmpty: isCatalogFiltersEmpty,
  // `hidden` is the toggle, not a chip.
  count: (f) => catalogFilterChips(f).length,
}

export interface CatalogChip { key: CatalogFilterKey; value: string; label: string; text: string }

/** One removable chip per active filter, except `q` (the search box shows it) and `hidden` (the toggle shows it). */
export function catalogFilterChips(f: CatalogFilters, publisherLabel?: (value: string) => string | undefined): CatalogChip[] {
  const out: CatalogChip[] = []
  for (const c of f.category ?? []) out.push({ key: 'category', value: c, label: 'Category', text: categoryLabel(c) })
  if (f.publisher) {
    const known = publisherLabel?.(f.publisher)
    out.push({ key: 'publisher', value: f.publisher, label: 'Publisher', text: known ?? (f.publisher === UNKNOWN_PUBLISHER ? 'Unknown' : f.publisher) })
  }
  if (f.unmatched) out.push({ key: 'unmatched', value: 'true', label: 'Status', text: 'Unmatched only' })
  return out
}

/** The filters with one chip removed. */
export function withoutChip(f: CatalogFilters, chip: Pick<CatalogChip, 'key' | 'value'>): CatalogFilters {
  const next: CatalogFilters = { ...f }
  if (chip.key === 'category') {
    const rest = (f.category ?? []).filter((c) => c !== chip.value)
    if (rest.length) next.category = rest
    else delete next.category
  } else {
    delete next[chip.key as 'publisher' | 'unmatched']
  }
  return next
}

/* ── URL state ───────────────────────────────────────────────────────────── */

export interface CatalogState {
  filters: CatalogFilters
  rejected: RejectedFilter[]
  sort: CatalogSort
  order: SortOrder
  page: number
  pageSize: number
}

/** Reads the page state; every missing or invalid value falls back to its default. A sort named without a direction takes that sort's own default. */
export function readCatalogState(params: URLSearchParams): CatalogState {
  const { filters, rejected } = parseCatalogFilters(params)
  const list = parseListState(params, CATALOG_SORT_KEYS) as CatalogListState
  const sort = list.sort ?? CATALOG_DEFAULT_SORT.sort
  const order = list.order ?? (list.sort ? defaultOrderFor(list.sort) : CATALOG_DEFAULT_SORT.order)
  return { filters, rejected, sort, order, page: list.page ?? 1, pageSize: list.page_size ?? CATALOG_DEFAULT_PAGE_SIZE }
}

/** The request for the data behind the page: filters plus explicit sort, order, page and page_size. */
export function catalogApiParams(state: Pick<CatalogState, 'filters' | 'sort' | 'order' | 'page' | 'pageSize'>): URLSearchParams {
  const out = writeCatalogFilters(state.filters, new URLSearchParams())
  out.set('sort', state.sort)
  out.set('order', state.order)
  out.set('page', String(state.page))
  out.set('page_size', String(state.pageSize))
  return out
}

/** The list part of the address: nothing for the default view, `sort` together with `order` otherwise. */
function writeList(next: URLSearchParams, list: { sort: CatalogSort; order: SortOrder; page: number; pageSize: number }) {
  for (const k of ['sort', 'order', 'page', 'page_size']) next.delete(k)
  if (list.sort !== CATALOG_DEFAULT_SORT.sort || list.order !== CATALOG_DEFAULT_SORT.order) {
    next.set('sort', list.sort)
    next.set('order', list.order)
  }
  if (list.page > 1) next.set('page', String(list.page))
  if (list.pageSize !== CATALOG_DEFAULT_PAGE_SIZE) next.set('page_size', String(list.pageSize))
}

/** New filters replace the old ones, written first in vocabulary order; the page goes back to 1. Everything else stays. */
export function withCatalogFilters(params: URLSearchParams, filters: CatalogFilters): URLSearchParams {
  const next = writeCatalogFilters(filters, new URLSearchParams())
  for (const [key, value] of params) {
    if ((CATALOG_FILTER_KEYS as readonly string[]).includes(key) || key === 'page') continue
    next.append(key, value)
  }
  return next
}

/** A column that is not the sort starts in its own default direction (counts biggest first); the same column flips. */
export function nextCatalogSort(current: { sort: string; order: SortOrder }, column: CatalogSort): { sort: CatalogSort; order: SortOrder } {
  if (current.sort === column) return { sort: column, order: current.order === 'asc' ? 'desc' : 'asc' }
  return { sort: column, order: defaultOrderFor(column) }
}

export function withCatalogSort(params: URLSearchParams, column: CatalogSort): URLSearchParams {
  const state = readCatalogState(params)
  const next = new URLSearchParams(params)
  writeList(next, { ...nextCatalogSort(state, column), page: 1, pageSize: state.pageSize })
  return next
}

export function withCatalogPage(params: URLSearchParams, page: number): URLSearchParams {
  const state = readCatalogState(params)
  const next = new URLSearchParams(params)
  writeList(next, { sort: state.sort, order: state.order, page: Math.max(1, Math.floor(page) || 1), pageSize: state.pageSize })
  return next
}

export function withCatalogPageSize(params: URLSearchParams, pageSize: number): URLSearchParams {
  const state = readCatalogState(params)
  const next = new URLSearchParams(params)
  writeList(next, { sort: state.sort, order: state.order, page: 1, pageSize: Math.max(1, Math.min(500, Math.floor(pageSize) || CATALOG_DEFAULT_PAGE_SIZE)) })
  return next
}

/** `/inventory/software?...` for a filter set (the view a footer or a chip link opens). */
export function catalogHref(filters: CatalogFilters, list: CatalogListState = {}): string {
  const params = writeCatalogFilters(filters, new URLSearchParams())
  const sort = list.sort ?? CATALOG_DEFAULT_SORT.sort
  const order = list.order ?? defaultOrderFor(sort)
  writeList(params, { sort, order, page: list.page ?? 1, pageSize: list.page_size ?? CATALOG_DEFAULT_PAGE_SIZE })
  const qs = params.toString()
  return qs ? `${SOFTWARE_PATH}?${qs}` : SOFTWARE_PATH
}

/** Whether typing in the search box alone should replace the history entry (a trail of near-identical entries helps nobody). */
export function catalogPushesHistory(prev: CatalogFilters, next: CatalogFilters): boolean {
  const same = (k: CatalogFilterKey) => JSON.stringify(prev[k] ?? null) === JSON.stringify(next[k] ?? null)
  return !(CATALOG_FILTER_KEYS.filter((k) => k !== 'q').every(same))
}

/* ── The hide toggle ─────────────────────────────────────────────────────── */

/** How a hidden category is named inside a sentence ("Hide updates, drivers..."). Unlisted ones fall back to their label. */
const HIDE_WORDS: Record<string, string> = { update: 'updates', driver: 'drivers', 'os-component': 'OS components', runtime: 'runtimes' }
/** The order people name them in: the default list (updates, drivers, OS components, runtimes), then the rest as the vocabulary lists them. */
const HIDE_ORDER = ['update', 'driver', 'os-component', 'runtime']

/** Categories in the order a sentence names them; duplicates and blanks are dropped. */
export function orderHiddenCategories(categories: readonly string[] | null | undefined): string[] {
  const set = new Set((categories ?? []).map((c) => String(c).trim().toLowerCase()).filter(Boolean))
  const rank = (c: string) => {
    const h = HIDE_ORDER.indexOf(c)
    if (h >= 0) return h
    const v = (SOFTWARE_CATEGORIES as readonly string[]).indexOf(c)
    return HIDE_ORDER.length + (v >= 0 ? v : SOFTWARE_CATEGORIES.length)
  }
  return [...set].sort((a, b) => rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0))
}

/** "updates, drivers and OS components": the categories as the plural nouns of a sentence. */
export function hiddenCategoryWords(categories: readonly string[] | null | undefined): string {
  const words = orderHiddenCategories(categories).map((c) => HIDE_WORDS[c] ?? categoryLabel(c).toLowerCase())
  if (words.length <= 1) return words.join('')
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
}

/** More categories than this and the switch names how many instead of listing them (the list is in its tooltip). */
export const HIDE_LABEL_MAX = 4
/** The label of the switch before the catalog (and so the list) has arrived, and when nothing is hidden by default. */
export const HIDE_LABEL_NEUTRAL = 'Hide categories hidden by default'

/**
 * The switch's label, built from `settings.hide_categories_by_default` (the catalog's `hidden_categories`), so it
 * never promises something the settings do not do (UI-03, DT-08): "Hide updates, drivers, OS components and
 * runtimes" by default, and whatever an administrator chose after that. Without a list (still loading, or nothing
 * hidden) the label is neutral, and a long list is counted rather than listed.
 */
export function hideSwitchLabel(hidden: readonly string[] | null | undefined): string {
  const ordered = orderHiddenCategories(hidden)
  if (ordered.length === 0) return HIDE_LABEL_NEUTRAL
  if (ordered.length > HIDE_LABEL_MAX) return `Hide ${ordered.length} categories hidden by default`
  return `Hide ${hiddenCategoryWords(ordered)}`
}

/** The categories as the notes under the catalog and the settings name them: "Update, Driver, OS component, Runtime". */
export function hiddenCategoryNames(hidden: readonly string[] | null | undefined): string {
  return orderHiddenCategories(hidden).map(categoryLabel).join(', ')
}

/** The switch is on when the catalog is asked to exclude the default-hidden categories. */
export function hideEnabled(filters: CatalogFilters): boolean {
  return filters.hidden !== 'include'
}

/** Switching it on returns to the default (`hidden=exclude` is never written); off writes `hidden=include`. */
export function withHideToggle(filters: CatalogFilters, hide: boolean): CatalogFilters {
  const next: CatalogFilters = { ...filters }
  if (hide) delete next.hidden
  else next.hidden = 'include'
  return next
}

/** Clearing the filters keeps the toggle: it is how the list is viewed, not something that narrows it. */
export function clearedFilters(filters: CatalogFilters): CatalogFilters {
  return filters.hidden ? { hidden: filters.hidden } : {}
}

/* ── Version bar ─────────────────────────────────────────────────────────── */

export interface BarSegment {
  kind: 'version' | 'other'
  key: string
  label: string
  hosts: number
  /** Share of the bar's width, 0 to 100 (of the sum of the per-version host counts). */
  width: number
  /** The list behind the number: absent for the bucket of several versions. */
  href?: string
  /** Position among the versions, 0 = first (drives the colour ramp). */
  index: number
  /** Tooltip and screen-reader text. No percentage: a server running two versions counts in both, so a share would mislead. */
  text: string
}

export interface VersionBarModel {
  segments: BarSegment[]
  /** Sum of the segments' hosts; widths are relative to it. */
  total: number
}

/**
 * The catalog row's version bar. `top` is in the server's order (newest first, the comparator's order); each
 * segment's hosts equal the total of `/assets?product=<key>&product_version=<version>`. `other` stands for
 * several versions and has no list of its own, so it is text. A product with no version at all has no bar.
 */
export function versionBarModel(productKey: string, bar: VersionBar): VersionBarModel {
  const segments: BarSegment[] = []
  const live = bar.top.filter((v) => v.hosts > 0)
  const total = live.reduce((s, v) => s + v.hosts, 0) + Math.max(0, bar.other.hosts)
  const width = (hosts: number) => (total > 0 ? (hosts / total) * 100 : 0)
  live.forEach((v, index) => {
    const label = v.version ?? 'Version unknown'
    segments.push({
      kind: 'version',
      key: `${v.scheme}:${v.version ?? ''}:${index}`,
      label,
      hosts: v.hosts,
      width: width(v.hosts),
      href: v.version ? assetsHref({ product: productKey, product_version: v.version }) : undefined,
      index,
      text: `${label}: ${plural(v.hosts, 'host')}`,
    })
  })
  if (bar.other.versions > 0 && bar.other.hosts > 0) {
    const label = `${fmtInt(bar.other.versions)} other ${bar.other.versions === 1 ? 'version' : 'versions'}`
    segments.push({
      kind: 'other',
      key: '__other__',
      label,
      hosts: bar.other.hosts,
      width: width(bar.other.hosts),
      index: live.length,
      text: `${label}: ${plural(bar.other.hosts, 'host')} across them`,
    })
  }
  return { segments, total }
}

/** Accessible name of a bar: how many versions, which is newest. */
export function versionBarLabel(row: Pick<CatalogRow, 'name' | 'version_count' | 'latest_version'>): string {
  if (row.version_count === 0) return `${row.name}: no version reported`
  const newest = row.latest_version ? `; newest ${row.latest_version}` : ''
  return `${row.name}: ${plural(row.version_count, 'version')}${newest}`
}

/* ── Rows ────────────────────────────────────────────────────────────────── */

/** The line under the product name on narrow screens, where the Publisher and Category columns are hidden. */
export function productSubline(row: Pick<CatalogRow, 'publisher' | 'category'>): string {
  return [row.publisher, categoryLabel(row.category)].filter(Boolean).join(' · ')
}

/** "2026-07-19" in the browser's time zone; missing is a dash, never a date. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const t = Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso.replace(' ', 'T')}Z`)
  if (!Number.isFinite(t)) return '—'
  const d = new Date(t)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** The moment of an ISO string in the browser's clock, or null when it is not a time. */
function instantOf(iso: string | null | undefined): number | null {
  if (!iso) return null
  const t = Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso.replace(' ', 'T')}Z`)
  return Number.isFinite(t) ? t : null
}

export interface FirstSeenView {
  /** The cell: a date, a dash, or `before <date>`. */
  text: string
  /** The explanation for the tooltip; absent when the date needs none. */
  title: string | undefined
  /** The value only says "already there when recording began": it is not the day the title first appeared. */
  before: boolean
}

/**
 * "First seen" of a software row or product. Change capture began on `historyStart` (`history_start` of
 * GET /inventory/changes): everything that was installed before has first-seen at or before that moment, and that
 * moment is when INVENTORY first saw it, not when the title was installed (DT-05, UP-06). Such a value is shown as
 * `before <capture date>` with a tooltip, never as a date that reads like an installation day; the earliest date
 * the machine itself reported (an install date) stays in the tooltip. Without a capture start the date is shown as
 * it is. A missing value is a dash.
 */
export function firstSeenView(iso: string | null | undefined, historyStart?: string | null): FirstSeenView {
  const at = instantOf(iso)
  if (at === null) return { text: '—', title: 'No first-seen date', before: false }
  const start = instantOf(historyStart)
  if (start === null || at > start) return { text: shortDate(iso), title: iso ?? undefined, before: false }
  const began = shortDate(historyStart)
  const known = shortDate(iso)
  return {
    text: `before ${began}`,
    title: known !== began
      ? `Already there when Inventory began recording on ${began}. The earliest date known is ${known}.`
      : `Already there when Inventory began recording on ${began}; the day it first appeared is not known.`,
    before: true,
  }
}

/** The first-seen words under a product's name. */
export function firstSeenText(iso: string | null | undefined, historyStart?: string | null): string {
  const v = firstSeenView(iso, historyStart)
  return v.text === '—' ? 'First seen unknown' : `First seen ${v.text}`
}

/** Accessible name of the Hosts number of a row ("Google Chrome: 41 servers"); the digits alone say nothing out of context. */
export function hostsLinkLabel(row: Pick<CatalogRow, 'name' | 'hosts'>): string {
  return `${row.name}: ${plural(row.hosts, 'server')}, open the list`
}

/** Accessible name of the Installs number of a row. */
export function installsLinkLabel(row: Pick<CatalogRow, 'name' | 'installs'>): string {
  return `${row.name}: ${plural(row.installs, 'installation')}, open the list`
}

/** Sentence for a screen reader and a row tooltip. */
export function rowSummary(row: CatalogRow): string {
  const status = row.norm_status === 'normalized' ? '' : `, ${normStatusMeta(row.norm_status).label.toLowerCase()}`
  return `${row.name}${status}: ${plural(row.hosts, 'host')}, ${plural(row.version_count, 'version')}`
}

/* ── Footer ──────────────────────────────────────────────────────────────── */

export interface CatalogFooterModel {
  /** Products in hidden categories are left out of the list. */
  hidden: null | {
    products: number
    installs: number
    categories: string[]
    /** The list that shows them too; its total is `showTotal`. */
    showHref: string
    showTotal: number
  }
  /** The list currently includes the hidden categories (the switch is off): offer the way back. */
  showingHidden: null | { categories: string[]; hideHref: string }
  unmatched: null | { count: number; href: string }
}

export function catalogFooter(resp: Pick<CatalogResponse, 'total' | 'hidden_products' | 'hidden_installs' | 'unmatched_titles' | 'hidden_categories'>, filters: CatalogFilters): CatalogFooterModel {
  const hiding = hideEnabled(filters)
  const hidden = hiding && resp.hidden_products > 0
    ? {
        products: resp.hidden_products,
        installs: resp.hidden_installs,
        categories: resp.hidden_categories,
        showHref: catalogHref(withHideToggle(filters, false)),
        showTotal: resp.total + resp.hidden_products,
      }
    : null
  const showingHidden = !hiding && resp.hidden_categories.length > 0
    ? { categories: resp.hidden_categories, hideHref: catalogHref(withHideToggle(filters, true)) }
    : null
  const unmatched = resp.unmatched_titles > 0 && !filters.unmatched
    ? { count: resp.unmatched_titles, href: catalogHref({ ...filters, unmatched: true }) }
    : null
  return { hidden, showingHidden, unmatched }
}

/* ── Publishers ──────────────────────────────────────────────────────────── */

export interface PublisherOption { value: string; label: string; products: number }

/**
 * The options of the publisher select. A publisher in the address that the facet does not list (beyond its limit,
 * or with no product left under the other filters) stays selectable so the chosen value is always visible.
 */
export function publisherOptions(resp: PublishersResponse | undefined, selected: string | undefined): PublisherOption[] {
  const items = (resp?.items ?? []).map((i) => ({ value: i.value, label: i.label, products: i.products }))
  if (selected && !items.some((i) => i.value.toLowerCase() === selected.toLowerCase())) {
    items.unshift({ value: selected, label: selected === UNKNOWN_PUBLISHER ? 'Unknown' : selected, products: 0 })
  }
  return items
}

/** The facet's filters: every catalog filter except `publisher` (so a count is what choosing the value would give). */
export function publisherQueryFilters(filters: CatalogFilters): CatalogFilters {
  const { publisher: _publisher, ...rest } = filters
  return rest
}

export const PUBLISHER_LIMIT = 200

/** "Top 200 of 340 publishers" when the facet is cut short. */
export function publisherNote(resp: PublishersResponse | undefined): string | null {
  if (!resp || resp.distinct <= resp.items.length) return null
  return `Showing the ${fmtInt(resp.items.length)} publishers with the most products, of ${fmtInt(resp.distinct)}. Search by publisher name to find the rest.`
}

/* ── Wording ─────────────────────────────────────────────────────────────── */

/**
 * The empty catalog of an appliance with no software list yet. Only the Windows agent reports installed software
 * (a server's list arrives with its first inventory report); a Linux server, or a server without the agent, does
 * not report software in this release (UI-02, UP-17, DT-09).
 */
export const NO_SOFTWARE_TEXT = "A server's software list arrives with the Windows agent's first inventory report. Linux servers and servers without the agent do not report software yet."

/** The empty catalog while the backend has rows it has not keyed yet: not an empty estate. */
export const NORMALISING_EMPTY_TEXT = 'Normalising existing software lists; this page fills in over the next minutes'

/** "1–50 of 26 products" for a screen reader live region. */
export function productRangeText(page: number, pageSize: number, total: number): string {
  if (total === 0) return '0 products'
  const first = (page - 1) * pageSize + 1
  const last = Math.min(total, page * pageSize)
  return `${fmtInt(first)} to ${fmtInt(last)} of ${fmtInt(total)} products`
}
