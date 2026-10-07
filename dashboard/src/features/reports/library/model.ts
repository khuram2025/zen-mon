/**
 * Report Library model: turns the catalog into library items and filters,
 * searches, sorts and groups them. Pure (no React, no storage access) so
 * scripts/test-report-library.mjs can exercise it in Node.
 */
import { PERIOD_LABEL } from '../periods'
import type {
  CatalogTemplate, CustomTemplate, PeriodId, ReportCatalog, ReportCategory, ReportRun, ScopeFilter,
  SectionDef, TemplateEntry,
} from '../types'
import type { RecentEntry, ReportRef } from '../storage'

/* ------------------------------------------------------------------ */
/*  Items                                                              */
/* ------------------------------------------------------------------ */

export type LibraryOrigin = 'builtin' | 'custom'

export interface LibraryItem {
  /** Favourite / recent reference: the built-in key, or `custom:<uuid>`. */
  ref: ReportRef
  origin: LibraryOrigin
  /** Viewer key: the built-in template key, or 'custom'. */
  key: string
  customId: string | null
  title: string
  description: string
  /** Category id from catalog.categories, or OTHER_CATEGORY. */
  category: string
  categoryTitle: string
  icon: string | null
  audience: string[]
  tags: string[]
  entries: TemplateEntry[]
  /** Resolved section titles, in template order. */
  sectionTitles: string[]
  defaultPeriod: PeriodId | null
  frequency: 'daily' | 'weekly' | 'monthly' | null
  featured: boolean
  formats: string[]
  filterable: ScopeFilter[]
  /** Permissions the template's sections read. */
  requires: string[]
  /** Some sections are hidden from this role (it still sees at least one, or may edit it). */
  partial: boolean
  /** Custom template whose sections this role cannot see at all (kept only because it may edit it). */
  restricted: boolean
  /** Position in the catalog (stable "relevance" order). */
  order: number
  // Organisation templates
  visibility: 'private' | 'shared' | null
  ownerName: string | null
  isOwner: boolean
  canEdit: boolean
  canDelete: boolean
  schedules: number
  sourceKey: string | null
  sourceTitle: string | null
  updatedAt: string | null
}

export const OTHER_CATEGORY = 'other'

export type SectionMap = Map<string, SectionDef>

export function sectionMap(catalog: Pick<ReportCatalog, 'sections'>): SectionMap {
  return new Map((catalog.sections || []).map((s) => [s.id, s]))
}

export function entryTitle(entry: TemplateEntry, sections: SectionMap): string {
  return (entry.title && entry.title.trim()) || sections.get(entry.id)?.title || entry.id
}

function entriesOf(t: { entries?: TemplateEntry[] | null; sections?: string[] | null }): TemplateEntry[] {
  if (t.entries && t.entries.length) return t.entries
  return (t.sections || []).map((id) => ({ id }))
}

function entryAvailable(entry: TemplateEntry, sections: SectionMap): boolean {
  if (entry.available === false) return false
  // Unknown sections (removed from the registry) are not counted as hidden.
  return sections.get(entry.id)?.available !== false
}

function unionRequires(entries: TemplateEntry[], sections: SectionMap): string[] {
  const out = new Set<string>()
  for (const e of entries) for (const p of sections.get(e.id)?.requires || []) out.add(p)
  return [...out].sort()
}

/**
 * Library items for a catalog: built-in section templates the role can see
 * at least one section of, then organisation templates. Legacy engine types
 * are not offered (templates replace their persona pages).
 */
export function buildLibraryItems(catalog: ReportCatalog): LibraryItem[] {
  const sections = sectionMap(catalog)
  const categories = new Map((catalog.categories || []).map((c) => [c.id, c]))
  const categoryOf = (id: string | null | undefined): { id: string; title: string } => {
    const c = id ? categories.get(id) : undefined
    return c ? { id: c.id, title: c.title } : { id: OTHER_CATEGORY, title: 'Other' }
  }
  const builtIns = (catalog.types || []).filter((t) => t.engine === 'sections')
  const byKey = new Map(builtIns.map((t) => [t.key, t]))
  const items: LibraryItem[] = []

  builtIns.forEach((t: CatalogTemplate, i) => {
    if (t.available === false) return
    const entries = entriesOf(t)
    if (entries.length && !entries.some((e) => entryAvailable(e, sections))) return
    const cat = categoryOf(t.category)
    items.push({
      ref: t.key,
      origin: 'builtin',
      key: t.key,
      customId: null,
      title: t.title,
      description: t.description || '',
      category: cat.id,
      categoryTitle: categories.has(t.category) ? cat.title : (t.category_title || cat.title),
      icon: t.icon || null,
      audience: t.audience || [],
      tags: t.tags || [],
      entries,
      sectionTitles: entries.map((e) => entryTitle(e, sections)),
      defaultPeriod: t.default_period ?? null,
      frequency: t.frequency ?? null,
      featured: !!t.featured,
      formats: t.formats || [],
      filterable: t.filterable || [],
      requires: t.requires?.length ? [...t.requires].sort() : unionRequires(entries, sections),
      partial: entries.some((e) => !entryAvailable(e, sections)),
      restricted: false,
      order: i,
      visibility: null,
      ownerName: null,
      isOwner: false,
      canEdit: false,
      canDelete: false,
      schedules: 0,
      sourceKey: null,
      sourceTitle: null,
      updatedAt: null,
    })
  })

  const base = builtIns.length
  ;(catalog.custom || []).forEach((c: CustomTemplate, i) => {
    const entries = entriesOf(c)
    const anyVisible = entries.length === 0 || entries.some((e) => entryAvailable(e, sections))
    // Hidden when the role cannot see any of its sections, unless it may still edit or delete it.
    if (!anyVisible && !c.can_edit && !c.can_delete) return
    const source = c.source_key ? byKey.get(c.source_key) : undefined
    const cat = categoryOf(c.category || source?.category)
    items.push({
      ref: `custom:${c.id}`,
      origin: 'custom',
      key: 'custom',
      customId: c.id,
      title: c.name,
      description: c.description || '',
      category: cat.id,
      categoryTitle: cat.title,
      icon: source?.icon || null,
      audience: source?.audience || [],
      tags: c.tags || [],
      entries,
      sectionTitles: entries.map((e) => entryTitle(e, sections)),
      defaultPeriod: c.default_period ?? source?.default_period ?? null,
      frequency: source?.frequency ?? null,
      featured: false,
      formats: source?.formats?.length ? source.formats : ['pdf', 'html', 'xlsx'],
      filterable: source?.filterable || [],
      requires: unionRequires(entries, sections),
      partial: anyVisible && entries.some((e) => !entryAvailable(e, sections)),
      restricted: !anyVisible,
      order: base + i,
      visibility: c.visibility,
      ownerName: c.owner_name,
      isOwner: !!c.is_owner,
      canEdit: !!c.can_edit,
      canDelete: !!c.can_delete,
      schedules: Number(c.schedules || 0),
      sourceKey: c.source_key,
      sourceTitle: source?.title ?? null,
      updatedAt: c.updated_at ?? c.created_at ?? null,
    })
  })
  return items
}

/* ------------------------------------------------------------------ */
/*  Filters and URL state                                              */
/* ------------------------------------------------------------------ */

export type LibraryViewId = 'all' | 'favorites' | 'recent' | 'mine' | 'shared' | 'builtin' | 'delivered'
export type LibrarySort = 'relevance' | 'name' | 'recent'
export type OriginFilter = 'all' | LibraryOrigin

export const LIBRARY_VIEWS: { id: LibraryViewId; label: string }[] = [
  { id: 'all', label: 'All reports' },
  { id: 'favorites', label: 'Favourites' },
  { id: 'recent', label: 'Recently viewed' },
  { id: 'mine', label: 'My templates' },
  { id: 'shared', label: 'Shared templates' },
  { id: 'builtin', label: 'Built-in' },
  { id: 'delivered', label: 'Delivered reports' },
]

export const SORTS: { id: LibrarySort; label: string }[] = [
  { id: 'relevance', label: 'Relevance' },
  { id: 'name', label: 'Name' },
  { id: 'recent', label: 'Recently used' },
]

export interface LibraryFilters {
  view: LibraryViewId
  /** A category id; only used with the 'all' view. */
  category: string | null
  q: string
  audience: string | null
  origin: OriginFilter
  sort: LibrarySort
}

export const DEFAULT_FILTERS: LibraryFilters = {
  view: 'all', category: null, q: '', audience: null, origin: 'all', sort: 'relevance',
}

export interface LibraryContext {
  favorites: ReportRef[]
  recent: RecentEntry[]
}

const VIEW_IDS = new Set<string>(LIBRARY_VIEWS.map((v) => v.id))
const SORT_IDS = new Set<string>(SORTS.map((s) => s.id))

export function filtersFromParams(params: URLSearchParams): LibraryFilters {
  const view = params.get('view') || 'all'
  const sort = params.get('sort') || 'relevance'
  const origin = params.get('origin')
  const category = params.get('category')
  const v: LibraryViewId = VIEW_IDS.has(view) ? (view as LibraryViewId) : 'all'
  return {
    view: v,
    category: v === 'all' && category ? category : null,
    q: (params.get('q') || '').slice(0, 200),
    audience: params.get('audience') || null,
    origin: origin === 'builtin' || origin === 'custom' ? origin : 'all',
    sort: SORT_IDS.has(sort) ? (sort as LibrarySort) : 'relevance',
  }
}

/** Only non-default values, so a plain /reports stays clean. */
export function filtersToParams(f: LibraryFilters): URLSearchParams {
  const p = new URLSearchParams()
  if (f.view !== 'all') p.set('view', f.view)
  if (f.view === 'all' && f.category) p.set('category', f.category)
  if (f.q.trim()) p.set('q', f.q)
  if (f.audience) p.set('audience', f.audience)
  if (f.origin !== 'all') p.set('origin', f.origin)
  if (f.sort !== 'relevance') p.set('sort', f.sort)
  return p
}

/** Search or a narrowing filter is active (the "All" shelves are hidden then). */
export function isFiltered(f: LibraryFilters): boolean {
  return !!f.q.trim() || !!f.audience || f.origin !== 'all'
}

/**
 * The unfiltered "All reports" overview (Featured and Recently viewed shelves,
 * then one section per category) — only in the card layout with the default
 * order. Searching, filtering, a category, another sort or the table layout
 * show one flat, ordered list instead.
 */
export function showsOverview(f: LibraryFilters, layout: 'grid' | 'table'): boolean {
  return f.view === 'all' && !f.category && !isFiltered(f) && f.sort === 'relevance' && layout === 'grid'
}

export function inView(item: LibraryItem, view: LibraryViewId, ctx: LibraryContext): boolean {
  switch (view) {
    case 'all': return true
    case 'favorites': return ctx.favorites.includes(item.ref)
    case 'recent': return ctx.recent.some((r) => r.ref === item.ref)
    case 'mine': return item.origin === 'custom' && item.isOwner
    case 'shared': return item.origin === 'custom' && item.visibility === 'shared'
    case 'builtin': return item.origin === 'builtin'
    case 'delivered': return false
  }
}

/** Items in the rail selection (view, or category), before search and filters. */
export function scopeItems(items: LibraryItem[], f: Pick<LibraryFilters, 'view' | 'category'>, ctx: LibraryContext): LibraryItem[] {
  return items.filter((i) => inView(i, f.view, ctx) && (f.view !== 'all' || !f.category || i.category === f.category))
}

/* ------------------------------------------------------------------ */
/*  Search                                                             */
/* ------------------------------------------------------------------ */

export function tokenize(q: string): string[] {
  return q.toLowerCase().split(/\s+/).map((t) => t.trim()).filter(Boolean).slice(0, 12)
}

const lc = (s: string | null | undefined) => (s || '').toLowerCase()

function tokenScore(item: LibraryItem, token: string): number {
  const title = lc(item.title)
  if (title.startsWith(token) || title.split(/[\s&/,()-]+/).some((w) => w.startsWith(token))) return 12
  if (title.includes(token)) return 8
  if (item.tags.some((t) => lc(t) === token)) return 6
  if (item.tags.some((t) => lc(t).includes(token))) return 5
  if (item.sectionTitles.some((t) => lc(t).includes(token))) return 4
  if (lc(item.categoryTitle).includes(token) || item.audience.some((a) => lc(a).includes(token))) return 3
  if (lc(item.description).includes(token)) return 2
  if (lc(item.ownerName).includes(token) || lc(item.sourceTitle).includes(token)) return 1
  return 0
}

/** 0 when any token matches nothing; otherwise higher is a better match. */
export function searchScore(item: LibraryItem, tokens: string[]): number {
  let total = 0
  for (const t of tokens) {
    const s = tokenScore(item, t)
    if (!s) return 0
    total += s
  }
  return total
}

/* ------------------------------------------------------------------ */
/*  Filter + sort                                                      */
/* ------------------------------------------------------------------ */

export function lastViewed(ref: ReportRef, recent: RecentEntry[]): number | null {
  const hit = recent.find((r) => r.ref === ref)
  return hit ? hit.at : null
}

const byTitle = (a: LibraryItem, b: LibraryItem) =>
  a.title.localeCompare(b.title, undefined, { sensitivity: 'base', numeric: true }) || a.order - b.order

export function sortItems(items: LibraryItem[], f: Pick<LibraryFilters, 'sort' | 'view' | 'q'>, ctx: LibraryContext): LibraryItem[] {
  const out = [...items]
  if (f.sort === 'name') return out.sort(byTitle)
  const seen = (i: LibraryItem) => lastViewed(i.ref, ctx.recent)
  if (f.sort === 'recent' || (f.sort === 'relevance' && f.view === 'recent' && !f.q.trim())) {
    return out.sort((a, b) => {
      const x = seen(a)
      const y = seen(b)
      if (x !== null && y !== null && x !== y) return y - x
      if (x !== null && y === null) return -1
      if (x === null && y !== null) return 1
      return byTitle(a, b)
    })
  }
  const tokens = tokenize(f.q)
  if (tokens.length) {
    const score = new Map(out.map((i) => [i.ref, searchScore(i, tokens)]))
    return out.sort((a, b) => (score.get(b.ref)! - score.get(a.ref)!) || Number(b.featured) - Number(a.featured) || a.order - b.order)
  }
  if (f.view === 'favorites') {
    const pos = (i: LibraryItem) => ctx.favorites.indexOf(i.ref)
    return out.sort((a, b) => pos(a) - pos(b))
  }
  return out.sort((a, b) => Number(b.featured) - Number(a.featured) || a.order - b.order)
}

export interface FilterResult {
  items: LibraryItem[]
  /** Items in the rail selection before search, audience and origin filters. */
  scopeTotal: number
}

export function applyFilters(all: LibraryItem[], f: LibraryFilters, ctx: LibraryContext): FilterResult {
  const scope = scopeItems(all, f, ctx)
  const tokens = tokenize(f.q)
  const items = scope.filter((i) =>
    (!f.audience || i.audience.includes(f.audience))
    && (f.origin === 'all' || i.origin === f.origin)
    && (!tokens.length || searchScore(i, tokens) > 0))
  return { items: sortItems(items, f, ctx), scopeTotal: scope.length }
}

/* ------------------------------------------------------------------ */
/*  Rail counts, shelves and groups                                    */
/* ------------------------------------------------------------------ */

export function viewCounts(items: LibraryItem[], ctx: LibraryContext): Record<Exclude<LibraryViewId, 'delivered'>, number> {
  const n = (v: LibraryViewId) => items.filter((i) => inView(i, v, ctx)).length
  return { all: items.length, favorites: n('favorites'), recent: n('recent'), mine: n('mine'), shared: n('shared'), builtin: n('builtin') }
}

export interface RailCategory extends Pick<ReportCategory, 'id' | 'title' | 'icon' | 'description'> {
  count: number
}

/** Every catalog category (with its item count), plus "Other" when something has no known category. */
export function railCategories(items: LibraryItem[], categories: ReportCategory[]): RailCategory[] {
  const counts = new Map<string, number>()
  for (const i of items) counts.set(i.category, (counts.get(i.category) || 0) + 1)
  const out: RailCategory[] = (categories || []).map((c) => ({ id: c.id, title: c.title, icon: c.icon, description: c.description, count: counts.get(c.id) || 0 }))
  const other = counts.get(OTHER_CATEGORY) || 0
  if (other) out.push({ id: OTHER_CATEGORY, title: 'Other', icon: 'FileText', description: 'Templates without a category.', count: other })
  return out
}

export interface CategoryGroup {
  category: RailCategory
  items: LibraryItem[]
}

/** Items grouped by category in catalog category order; empty groups dropped. */
export function groupByCategory(items: LibraryItem[], categories: ReportCategory[]): CategoryGroup[] {
  return railCategories(items, categories)
    .map((category) => ({ category, items: items.filter((i) => i.category === category.id) }))
    .filter((g) => g.items.length > 0)
}

/** The Featured shelf: six fills two or three even rows at every grid width. */
export const FEATURED_LIMIT = 6

export function featuredItems(items: LibraryItem[], limit = FEATURED_LIMIT): LibraryItem[] {
  return items.filter((i) => i.featured).sort((a, b) => a.order - b.order).slice(0, limit)
}

/** Starred templates in the order they were starred (newest first); stale stars are skipped. */
export function favoriteItems(items: LibraryItem[], favorites: ReportRef[], limit = FEATURED_LIMIT): LibraryItem[] {
  const byRef = new Map(items.map((i) => [i.ref, i]))
  const out: LibraryItem[] = []
  for (const ref of favorites) {
    const item = byRef.get(ref)
    if (item && !out.includes(item)) out.push(item)
    if (out.length >= limit) break
  }
  return out
}

export function recentItems(items: LibraryItem[], recent: RecentEntry[], limit = 6): { item: LibraryItem; at: number }[] {
  const byRef = new Map(items.map((i) => [i.ref, i]))
  const out: { item: LibraryItem; at: number }[] = []
  for (const r of [...recent].sort((a, b) => b.at - a.at)) {
    const item = byRef.get(r.ref)
    if (item && !out.some((o) => o.item.ref === r.ref)) out.push({ item, at: r.at })
    if (out.length >= limit) break
  }
  return out
}

/** Audiences present in the library, in the server's audience order. */
export const AUDIENCE_ORDER = [
  'Executive', 'IT manager', 'Network engineer', 'NOC operator', 'Systems engineer',
  'Application owner', 'Service owner', 'Security & audit', 'Capacity planner',
]

export function audienceOptions(items: LibraryItem[]): string[] {
  const seen = new Set<string>()
  for (const i of items) for (const a of i.audience) seen.add(a)
  const known = AUDIENCE_ORDER.filter((a) => seen.has(a))
  const extra = [...seen].filter((a) => !AUDIENCE_ORDER.includes(a)).sort((a, b) => a.localeCompare(b))
  return [...known, ...extra]
}

/* ------------------------------------------------------------------ */
/*  Presentation helpers                                               */
/* ------------------------------------------------------------------ */

export function periodLabel(p: string | null | undefined): string | null {
  if (!p) return null
  return PERIOD_LABEL[p] || p
}

export const FREQUENCY_LABEL: Record<string, string> = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' }

/**
 * "Viewed just now / 5 min ago / 3 h ago / yesterday / 4 days ago" for a
 * browser-recorded timestamp (both instants come from this browser's clock).
 * Older than a week returns null so the caller can print the date instead.
 */
export function viewedAgo(at: number, now = Date.now()): string | null {
  const s = Math.max(0, Math.round((now - at) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.floor(h / 24)
  if (d === 1) return 'yesterday'
  if (d < 7) return `${d} days ago`
  return null
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** "8 sections · Last 7 days · Executive" */
export function metaLine(item: LibraryItem): string {
  const parts = [plural(item.entries.length, 'section')]
  const period = periodLabel(item.defaultPeriod)
  if (period) parts.push(period)
  if (item.audience.length) parts.push(item.audience.length > 1 ? `${item.audience[0]} +${item.audience.length - 1}` : item.audience[0])
  return parts.join(' · ')
}

export const FORMAT_LABEL: Record<string, string> = { pdf: 'PDF', html: 'HTML', xlsx: 'Excel', excel: 'Excel', csv: 'CSV' }

/** Readable names for the module permissions sections read. */
export const PERMISSION_LABEL: Record<string, string> = {
  'devices.view': 'Devices',
  'service_checks.view': 'Services & web checks',
  'netflow.view': 'NetFlow traffic',
  'alerts.view': 'Alerts',
  'servers.view': 'Servers',
  'apm.view': 'Applications (APM)',
  'ncm.view': 'Configuration (NCCM)',
  'udt.view': 'Device Tracker',
  'udt.view_users': 'Device Tracker user logins',
  'netpath.view': 'Network paths',
  'maps.view': 'Maps',
  'discovery.view': 'Discovery',
  'reports.view': 'Reports',
  'users.view': 'Users',
  'settings.view': 'Settings',
}

export function permissionLabel(p: string): string {
  return PERMISSION_LABEL[p] || p
}

export const SCOPE_LABEL: Record<string, string> = {
  devices: 'Devices',
  applications: 'Applications',
  netflow: 'NetFlow filter',
}

/**
 * One-line summary of a placed section's options, using the section's option
 * schema: "Top 10 · Sort by: Downtime · Hide if empty". Booleans appear only
 * when on; at most `max` parts.
 */
export function optionSummary(entry: TemplateEntry, def: SectionDef | undefined, max = 3): string {
  if (!def || !def.options?.length) return ''
  const values = entry.options || {}
  const parts: string[] = []
  for (const opt of def.options) {
    const has = Object.prototype.hasOwnProperty.call(values, opt.key)
    const v = has ? values[opt.key] : opt.default
    if (v === null || v === undefined || v === '') continue
    if (opt.type === 'bool') {
      if (v === true) parts.push(opt.label)
      continue
    }
    if (opt.type === 'select') {
      const choice = opt.choices?.find(([value]) => String(value) === String(v))
      parts.push(`${opt.label}: ${choice ? choice[1] : String(v)}`)
      continue
    }
    const label = opt.label.trim()
    // "Top N" style labels read naturally with the number appended.
    parts.push(/^top\b/i.test(label) && !/:/.test(label) ? `${label} ${v}` : `${label}: ${v}`)
  }
  return parts.slice(0, max).join(' · ')
}

/* ------------------------------------------------------------------ */
/*  Links and actions                                                  */
/* ------------------------------------------------------------------ */

export function openLink(item: Pick<LibraryItem, 'origin' | 'key' | 'customId'>): string {
  return item.origin === 'custom' && item.customId
    ? `/reports/view/custom?custom_id=${encodeURIComponent(item.customId)}`
    : `/reports/view/${encodeURIComponent(item.key)}`
}

/** Customize = start the designer from a copy (built-ins stay read-only). */
export function customizeLink(item: Pick<LibraryItem, 'origin' | 'key' | 'customId'>): string {
  return item.origin === 'custom' && item.customId
    ? `/reports/builder?clone=${encodeURIComponent(item.customId)}`
    : `/reports/builder?from=${encodeURIComponent(item.key)}`
}

export function editLink(item: Pick<LibraryItem, 'customId'>): string | null {
  return item.customId ? `/reports/builder?edit=${encodeURIComponent(item.customId)}` : null
}

/**
 * What ReportScheduleFormDialog's `prefill` receives from the library. Only
 * the fields the library knows: custom_report_id only for organisation
 * templates, period only when the template suggests one (the dialog keeps its
 * own default otherwise).
 */
export interface SchedulePrefill {
  /** Suggested schedule name. */
  name?: string
  report_type: string
  custom_report_id?: string
  period?: PeriodId
}

/** The period a delivery cadence usually covers: the previous full day, week or month. */
export const FREQUENCY_PERIOD: Record<'daily' | 'weekly' | 'monthly', PeriodId> = {
  daily: 'yesterday',
  weekly: 'previous_week',
  monthly: 'previous_month',
}

export function schedulePrefill(
  item: Pick<LibraryItem, 'origin' | 'key' | 'customId' | 'defaultPeriod' | 'frequency' | 'title'>,
): SchedulePrefill {
  const period = item.defaultPeriod || (item.frequency ? FREQUENCY_PERIOD[item.frequency] : undefined)
  const out: SchedulePrefill = item.origin === 'custom' && item.customId
    ? { name: item.title, report_type: 'custom', custom_report_id: item.customId }
    : { name: item.title, report_type: item.key }
  if (period) out.period = period
  return out
}

/** The schedule count from a 409 "3 schedule(s) deliver this template" reply. */
export function conflictScheduleCount(message: string | null | undefined): number | null {
  const m = /(\d+)\s+schedule/i.exec(message || '')
  return m ? Number(m[1]) : null
}

/* ------------------------------------------------------------------ */
/*  Delivered reports                                                  */
/* ------------------------------------------------------------------ */

export type RunTone = 'success' | 'warning' | 'danger' | 'neutral'

export function runTone(status: string | null | undefined): RunTone {
  const s = lc(status)
  if (s === 'success' || s === 'delivered' || s === 'ok' || s === 'completed') return 'success'
  if (s === 'partial' || s === 'skipped' || s === 'no_recipients') return 'warning'
  if (s === 'failed' || s === 'error') return 'danger'
  return 'neutral'
}

export function filterRuns(runs: ReportRun[], q: string): ReportRun[] {
  const tokens = tokenize(q)
  if (!tokens.length) return runs
  return runs.filter((r) => {
    const hay = [r.title, r.schedule_name, r.report_type, r.status, periodLabel(r.period)].map(lc).join(' \u0000 ')
    return tokens.every((t) => hay.includes(t))
  })
}

export function sharedRunLink(token: string | null | undefined): string | null {
  return token ? `/api/v1/reports/shared/${encodeURIComponent(token)}` : null
}
