/**
 * Template designer model: the draft being edited and the pure helpers the
 * designer UI is built on (entry reordering, option defaults and summaries,
 * validation, payloads and library search).
 *
 * No React and no runtime imports (types only), so
 * scripts/test-report-designer.mjs can load this file directly.
 */
import type {
  CatalogTemplate, CustomTemplate, PeriodId, ReportCategory, SectionDef, SectionOption, TemplateBody,
  TemplateEntry,
} from '../types'

/* ------------------------------------------------------------------ */
/*  Limits (mirror server/app/api/v1/reports.py)                        */
/* ------------------------------------------------------------------ */

export const MAX_SECTIONS = 40
export const NAME_MAX = 120
export const DESCRIPTION_MAX = 2000
export const TITLE_MAX = 120
export const TAGS_MAX = 12
export const TAG_LEN_MAX = 32

/* ------------------------------------------------------------------ */
/*  Draft                                                               */
/* ------------------------------------------------------------------ */

/** One section placed in the draft. `uid` is local only (React keys, selection, drag and drop). */
export interface DesignerEntry {
  uid: string
  id: string
  /** Title override; empty means the section's own title. */
  title: string
  options: Record<string, unknown>
}

export interface Draft {
  name: string
  description: string
  category: string | null
  tags: string[]
  visibility: 'private' | 'shared'
  defaultPeriod: PeriodId | null
  sourceKey: string | null
  entries: DesignerEntry[]
}

/** blank: /reports/builder · from: ?from=<built-in> · edit: ?edit=<uuid> · clone: ?clone=<uuid> */
export type DesignerMode = 'new' | 'from' | 'edit' | 'clone'

export type SectionIndex = Record<string, SectionDef | undefined>

let uidSeq = 0
export function newUid(): string {
  uidSeq += 1
  return `e${Date.now().toString(36)}${uidSeq.toString(36)}`
}

const clip = (s: string | null | undefined, max: number) => (s ?? '').slice(0, max)

export function emptyDraft(): Draft {
  return {
    name: '',
    description: '',
    category: null,
    tags: [],
    visibility: 'private',
    defaultPeriod: 'last_7d',
    sourceKey: null,
    entries: [],
  }
}

/** Trimmed, de-duplicated (case-insensitive) tags within the server's limits. */
export function cleanTags(tags: readonly unknown[] | null | undefined): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const t of tags ?? []) {
    if (typeof t !== 'string') continue
    const v = t.trim().replace(/\s+/g, ' ')
    if (!v || v.length > TAG_LEN_MAX || seen.has(v.toLowerCase())) continue
    seen.add(v.toLowerCase())
    out.push(v)
    if (out.length >= TAGS_MAX) break
  }
  return out
}

/** Template entries (or bare section ids for older templates) as designer entries. */
export function entriesFrom(
  items: readonly TemplateEntry[] | null | undefined,
  fallbackIds: readonly string[] | null | undefined = [],
  makeUid: () => string = newUid,
): DesignerEntry[] {
  const source: TemplateEntry[] = items && items.length ? [...items] : (fallbackIds ?? []).map((id) => ({ id }))
  return source
    .filter((e) => e && typeof e.id === 'string' && e.id)
    .map((e) => ({
      uid: makeUid(),
      id: e.id,
      title: typeof e.title === 'string' ? clip(e.title.trim(), TITLE_MAX) : '',
      options: e.options && typeof e.options === 'object' ? { ...e.options } : {},
    }))
}

/** "Customize" a built-in template: a new, private organisation template based on it. */
export function draftFromBuiltin(t: CatalogTemplate, makeUid: () => string = newUid): Draft {
  return {
    name: clip(`${t.title} (custom)`, NAME_MAX),
    description: clip(t.description, DESCRIPTION_MAX),
    category: t.category || null,
    tags: cleanTags(t.tags),
    visibility: 'private',
    defaultPeriod: t.default_period ?? null,
    sourceKey: t.key,
    entries: entriesFrom(t.entries, t.sections, makeUid),
  }
}

/** Edit an organisation template, or start a private copy of it. */
export function draftFromCustom(c: CustomTemplate, mode: 'edit' | 'clone', makeUid: () => string = newUid): Draft {
  return {
    name: mode === 'clone' ? clip(`${c.name} (copy)`, NAME_MAX) : clip(c.name, NAME_MAX),
    description: clip(c.description, DESCRIPTION_MAX),
    category: c.category || null,
    tags: cleanTags(c.tags),
    visibility: mode === 'clone' ? 'private' : c.visibility,
    defaultPeriod: c.default_period ?? null,
    sourceKey: c.source_key ?? null,
    entries: entriesFrom(c.entries, c.sections, makeUid),
  }
}

/* ------------------------------------------------------------------ */
/*  Entry list operations (all return new arrays)                       */
/* ------------------------------------------------------------------ */

/** Move the item at `from` to index `to` (clamped). */
export function moveEntry<T>(list: readonly T[], from: number, to: number): T[] {
  const next = list.slice()
  if (from < 0 || from >= list.length) return next
  const target = Math.max(0, Math.min(list.length - 1, to))
  if (target === from) return next
  const [item] = next.splice(from, 1)
  next.splice(target, 0, item)
  return next
}

/**
 * Final index of an item dragged from `from` and dropped in insertion slot
 * `slot` (0 = before the first item, length = after the last).
 */
export function dropIndex(from: number, slot: number): number {
  return slot > from ? slot - 1 : slot
}

/** Insert at `at` (clamped); appends when `at` is omitted. */
export function insertEntry<T>(list: readonly T[], item: T, at?: number): T[] {
  const next = list.slice()
  const i = at == null ? next.length : Math.max(0, Math.min(next.length, at))
  next.splice(i, 0, item)
  return next
}

export function removeEntry<T>(list: readonly T[], index: number): T[] {
  if (index < 0 || index >= list.length) return list.slice()
  return [...list.slice(0, index), ...list.slice(index + 1)]
}

/** A copy of the entry at `index`, placed right after it. */
export function duplicateEntry(list: readonly DesignerEntry[], index: number, uid: string = newUid()): DesignerEntry[] {
  const src = list[index]
  if (!src) return list.slice()
  return insertEntry(list, { ...src, uid, options: JSON.parse(JSON.stringify(src.options ?? {})) }, index + 1)
}

/** Where a newly added section goes: after the selected entry, else at the end. */
export function addPosition(list: readonly { uid: string }[], selectedUid: string | null | undefined): number {
  const i = selectedUid ? list.findIndex((e) => e.uid === selectedUid) : -1
  return i >= 0 ? i + 1 : list.length
}

/** "<title>, position 3 of 8" — keyboard reordering announcements. */
export function positionText(title: string, index: number, total: number): string {
  return `${title}, position ${index + 1} of ${total}`
}

/** Display title of an entry: its override, else the section's title, else its id. */
export function entryTitle(entry: Pick<DesignerEntry, 'id' | 'title'>, def?: Pick<SectionDef, 'title'>): string {
  return entry.title.trim() || def?.title || entry.id
}

/** How many entries use each section id. */
export function usageCounts(list: readonly { id: string }[]): Record<string, number> {
  const out: Record<string, number> = {}
  for (const e of list) out[e.id] = (out[e.id] ?? 0) + 1
  return out
}

/**
 * Index in a rendered report of the entry at `entryIndex`: the n-th section
 * with the same section id, so sections omitted for scope do not shift it.
 */
export function previewIndexFor(
  entries: readonly { id: string }[],
  entryIndex: number,
  sections: readonly { id: string; section_id?: string }[],
): number {
  const target = entries[entryIndex]
  if (!target) return -1
  let nth = 0
  for (let i = 0; i < entryIndex; i++) if (entries[i].id === target.id) nth++
  for (let i = 0; i < sections.length; i++) {
    if ((sections[i].section_id ?? sections[i].id) !== target.id) continue
    if (nth === 0) return i
    nth--
  }
  return -1
}

/* ------------------------------------------------------------------ */
/*  Options                                                             */
/* ------------------------------------------------------------------ */

type OptionsDef = Pick<SectionDef, 'options'> | undefined

export function defaultOptions(def: OptionsDef): Record<string, unknown> {
  return Object.fromEntries((def?.options ?? []).map((o) => [o.key, o.default]))
}

/** Defaults overlaid with the entry's values for known keys. */
export function effectiveOptions(def: OptionsDef, options: Record<string, unknown> | undefined): Record<string, unknown> {
  const out = defaultOptions(def)
  for (const o of def?.options ?? []) if (options && o.key in options && options[o.key] !== undefined) out[o.key] = options[o.key]
  return out
}

function sameValue(a: unknown, b: unknown): boolean {
  if (typeof a === 'number' && typeof b === 'number') return a === b || (Number.isNaN(a) && Number.isNaN(b))
  return a === b
}

/**
 * Only the known options that differ from the section's defaults, so
 * templates keep following improved defaults. Numeric strings become numbers.
 */
export function sparseOptions(def: OptionsDef, options: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (!def) return { ...(options ?? {}) }
  for (const o of def.options) {
    if (!options || !(o.key in options)) continue
    let v = options[o.key]
    if ((o.type === 'int' || o.type === 'number') && typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) v = Number(v)
    if (!sameValue(v, o.default)) out[o.key] = v
  }
  return out
}

/** True when any known option differs from its default. */
export function isCustomized(def: OptionsDef, options: Record<string, unknown> | undefined): boolean {
  return Object.keys(sparseOptions(def, options)).length > 0
}

function rangeText(o: SectionOption): string {
  if (o.min != null && o.max != null) return `between ${o.min} and ${o.max}`
  if (o.min != null) return `at least ${o.min}`
  if (o.max != null) return `at most ${o.max}`
  return ''
}

/** The problem with one option value, or null when it is valid. */
export function optionError(o: SectionOption, value: unknown): string | null {
  if (o.type === 'int' || o.type === 'number') {
    const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() !== '' ? Number(value) : Number.NaN
    if (!Number.isFinite(n)) return 'Enter a number'
    if (o.type === 'int' && !Number.isInteger(n)) return 'Use a whole number'
    if ((o.min != null && n < o.min) || (o.max != null && n > o.max)) return `Use a value ${rangeText(o)}`
    return null
  }
  if (o.type === 'select') {
    return (o.choices ?? []).some(([v]) => v === value) ? null : 'Choose one of the listed values'
  }
  if (o.type === 'bool') return typeof value === 'boolean' ? null : 'Turn this on or off'
  return null
}

/** Problems by option key (only invalid options appear). */
export function optionErrors(def: OptionsDef, options: Record<string, unknown> | undefined): Record<string, string> {
  const eff = effectiveOptions(def, options)
  const out: Record<string, string> = {}
  for (const o of def?.options ?? []) {
    const err = optionError(o, eff[o.key])
    if (err) out[o.key] = err
  }
  return out
}

/** Split "Packet loss threshold (%)" into a label and a short unit. */
function splitUnit(label: string): { base: string; unit: string } {
  const m = /^(.*?)\s*\(([^)]+)\)\s*$/.exec(label)
  if (!m) return { base: label, unit: '' }
  return { base: m[1], unit: m[2].length <= 8 ? m[2] : '' }
}

function numberText(v: unknown): string {
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(4)))
  return String(v ?? '—')
}

/** One option as summary text, or '' when it says nothing (a switched-off flag that is off by default). */
export function optionPhrase(o: SectionOption, value: unknown): string {
  if (o.key === 'top_n') return `Top ${numberText(value)}`
  if (o.type === 'select') {
    const choice = (o.choices ?? []).find(([v]) => v === value)
    return `${o.label} ${choice ? choice[1] : String(value ?? '—')}`
  }
  if (o.type === 'bool') {
    if (value === true) return o.label
    return o.default === true ? `${o.label}: off` : ''
  }
  const { base, unit } = splitUnit(o.label)
  const n = numberText(value)
  return unit === '%' ? `${base} ${n}%` : unit ? `${base} ${n} ${unit}` : `${base} ${n}`
}

/** "Top 10 · Rank by RTT · Packet loss threshold 1%" (top_n first; at most `max` parts). */
export function optionSummary(def: OptionsDef, options: Record<string, unknown> | undefined, max = 3): string {
  const eff = effectiveOptions(def, options)
  const ordered = [...(def?.options ?? [])].sort((a, b) => Number(b.key === 'top_n') - Number(a.key === 'top_n'))
  const parts = ordered.map((o) => optionPhrase(o, eff[o.key])).filter(Boolean)
  if (parts.length <= max) return parts.join(' · ')
  return `${parts.slice(0, max).join(' · ')} · +${parts.length - max} more`
}

/** Step for the ± buttons of a numeric option. */
export function optionStep(o: SectionOption): number {
  if (o.type === 'int') return 1
  if (o.max != null && o.min != null && o.max - o.min <= 10) return 0.1
  return 1
}

/** Value after pressing + (dir 1) or − (dir -1), clamped to the option's range. */
export function stepValue(o: SectionOption, value: unknown, dir: 1 | -1): number {
  const step = optionStep(o)
  const base = typeof value === 'number' && Number.isFinite(value) ? value
    : typeof o.default === 'number' ? o.default : o.min ?? 0
  let next = Math.round((base + dir * step) / step) * step
  next = Number(next.toFixed(6))
  if (o.min != null) next = Math.max(o.min, next)
  if (o.max != null) next = Math.min(o.max, next)
  return o.type === 'int' ? Math.round(next) : next
}

/* ------------------------------------------------------------------ */
/*  Tags                                                                */
/* ------------------------------------------------------------------ */

export interface TagResult {
  tags: string[]
  /** Text that could not be added (left in the input so it can be fixed). */
  rest: string
  error: string | null
}

/** Add comma-separated tags from `input`, enforcing the length, count and duplicate rules. */
export function addTags(existing: readonly string[], input: string): TagResult {
  const tags = existing.slice()
  const rest: string[] = []
  let error: string | null = null
  for (const raw of input.split(',')) {
    const t = raw.trim().replace(/\s+/g, ' ')
    if (!t) continue
    if (t.length > TAG_LEN_MAX) {
      error ??= `Tags are at most ${TAG_LEN_MAX} characters`
      rest.push(t)
    } else if (tags.some((x) => x.toLowerCase() === t.toLowerCase())) {
      error ??= `"${t}" is already a tag`
    } else if (tags.length >= TAGS_MAX) {
      error ??= `Use at most ${TAGS_MAX} tags`
      rest.push(t)
    } else {
      tags.push(t)
    }
  }
  return { tags, rest: rest.join(', '), error }
}

/* ------------------------------------------------------------------ */
/*  Validation                                                          */
/* ------------------------------------------------------------------ */

export type DraftField = 'name' | 'description' | 'tags' | 'sections' | 'category' | 'defaultPeriod'

export type IssueTarget =
  | { kind: 'field'; field: DraftField }
  | { kind: 'entry'; uid: string; option?: string }

export interface Issue {
  key: string
  message: string
  target: IssueTarget
}

export interface EntryProblems {
  /** The section no longer exists in the catalog. */
  unknown?: string
  title?: string
  options: Record<string, string>
}

export interface Validation {
  valid: boolean
  issues: Issue[]
  fields: Partial<Record<DraftField, string>>
  entries: Record<string, EntryProblems>
  /** Entries whose options are invalid (the preview cannot render them). */
  optionIssueCount: number
}

export function validateDraft(
  draft: Draft,
  sections: SectionIndex,
  known: { categories?: readonly string[]; periods?: readonly string[] } = {},
): Validation {
  const issues: Issue[] = []
  const fields: Partial<Record<DraftField, string>> = {}
  const entries: Record<string, EntryProblems> = {}
  let optionIssueCount = 0
  const field = (f: DraftField, message: string) => {
    fields[f] ??= message
    issues.push({ key: `field:${f}`, message, target: { kind: 'field', field: f } })
  }

  const name = draft.name.trim()
  if (!name) field('name', 'Give the template a name')
  else if (draft.name.length > NAME_MAX) field('name', `Keep the name to ${NAME_MAX} characters (now ${draft.name.length})`)
  if (draft.description.length > DESCRIPTION_MAX) {
    field('description', `Keep the description to ${DESCRIPTION_MAX} characters (now ${draft.description.length})`)
  }
  if (draft.tags.length > TAGS_MAX) field('tags', `Use at most ${TAGS_MAX} tags`)
  else if (draft.tags.some((t) => !t.trim() || t.length > TAG_LEN_MAX)) field('tags', `Tags are 1–${TAG_LEN_MAX} characters`)
  if (draft.category && known.categories && !known.categories.includes(draft.category)) {
    field('category', 'This category no longer exists; choose another')
  }
  if (draft.defaultPeriod && known.periods && !known.periods.includes(draft.defaultPeriod)) {
    field('defaultPeriod', 'Choose a default period from the list')
  }

  if (draft.entries.length === 0) field('sections', 'Add at least one section')
  else if (draft.entries.length > MAX_SECTIONS) {
    field('sections', `A template holds at most ${MAX_SECTIONS} sections; remove ${draft.entries.length - MAX_SECTIONS}`)
  }

  draft.entries.forEach((e, i) => {
    const def = sections[e.id]
    const where = `Section ${i + 1}`
    const p: EntryProblems = { options: {} }
    if (!def) {
      p.unknown = `"${e.id}" is no longer available; remove it`
      issues.push({ key: `entry:${e.uid}`, message: `${where}: ${p.unknown}`, target: { kind: 'entry', uid: e.uid } })
    } else {
      const label = entryTitle(e, def)
      if (e.title.length > TITLE_MAX) {
        p.title = `Keep the title to ${TITLE_MAX} characters`
        issues.push({ key: `entry:${e.uid}:title`, message: `${where} (${label}): ${p.title}`, target: { kind: 'entry', uid: e.uid } })
      }
      p.options = optionErrors(def, e.options)
      const keys = Object.keys(p.options)
      if (keys.length) optionIssueCount++
      for (const k of keys) {
        const o = def.options.find((x) => x.key === k)
        issues.push({
          key: `entry:${e.uid}:${k}`,
          message: `${where} (${label}): ${o?.label ?? k} — ${p.options[k].charAt(0).toLowerCase()}${p.options[k].slice(1)}`,
          target: { kind: 'entry', uid: e.uid, option: k },
        })
      }
    }
    if (p.unknown || p.title || Object.keys(p.options).length) entries[e.uid] = p
  })

  return { valid: issues.length === 0, issues, fields, entries, optionIssueCount }
}

/* ------------------------------------------------------------------ */
/*  Payloads                                                            */
/* ------------------------------------------------------------------ */

/** Template entries as the API takes them (no local ids; sparse options). */
export function toEntries(list: readonly DesignerEntry[], sections: SectionIndex): TemplateEntry[] {
  return list.map((e) => {
    const out: TemplateEntry = { id: e.id }
    const title = e.title.trim()
    if (title) out.title = title
    const opts = sparseOptions(sections[e.id], e.options)
    if (Object.keys(opts).length) out.options = opts
    return out
  })
}

/** Body for POST /reports/custom and PUT /reports/custom/{id}. */
export function toTemplateBody(draft: Draft, sections: SectionIndex): TemplateBody {
  return {
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    sections: toEntries(draft.entries, sections),
    category: draft.category || null,
    tags: cleanTags(draft.tags),
    visibility: draft.visibility,
    source_key: draft.sourceKey || null,
    default_period: draft.defaultPeriod || null,
  }
}

/** A stable fingerprint of what would be saved (dirty tracking). */
export function draftSignature(draft: Draft, sections: SectionIndex): string {
  const body = toTemplateBody(draft, sections)
  return JSON.stringify({ ...body, description: body.description ?? '', tags: body.tags ?? [] })
}

/* ------------------------------------------------------------------ */
/*  Section library                                                     */
/* ------------------------------------------------------------------ */

export interface SectionGroup {
  category: string
  sections: SectionDef[]
}

const norm = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')

/** Every word of the query appears in the title, description, category, id or permissions. */
export function matchesQuery(s: Pick<SectionDef, 'id' | 'title' | 'description' | 'category' | 'requires'>, query: string): boolean {
  const words = norm(query).split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const hay = norm([s.title, s.description, s.category, s.id.replace(/_/g, ' '), ...(s.requires ?? [])].join(' '))
  return words.every((w) => hay.includes(w))
}

/** Sections matching `query`, grouped by category in catalog order. */
export function groupSections(sections: readonly SectionDef[], query = ''): SectionGroup[] {
  const groups: SectionGroup[] = []
  const byName = new Map<string, SectionGroup>()
  for (const s of sections) {
    if (!matchesQuery(s, query)) continue
    const cat = s.category || 'Other'
    let g = byName.get(cat)
    if (!g) {
      g = { category: cat, sections: [] }
      byName.set(cat, g)
      groups.push(g)
    }
    g.sections.push(s)
  }
  return groups
}

/** The catalog category a section category label belongs to (for its icon), if any. */
export function catalogCategoryFor(label: string, categories: readonly ReportCategory[]): ReportCategory | undefined {
  const l = norm(label.trim())
  if (!l) return undefined
  return categories.find((c) => norm(c.id) === l)
    ?? categories.find((c) => norm(c.title).startsWith(l))
    ?? categories.find((c) => norm(c.title).split(/[^a-z]+/).includes(l))
    ?? categories.find((c) => l.length >= 5 && norm(c.id).startsWith(l.slice(0, 5)))
}

const PERMISSION_MODULES: Record<string, string> = {
  devices: 'Devices & Monitoring', discovery: 'Discovery', alerts: 'Alerting', service_checks: 'Service Checks',
  netflow: 'NetFlow', udt: 'User Device Tracker', netpath: 'Network Path', ncm: 'Config Backup (NCM)',
  servers: 'Servers & Agents', apm: 'Applications (APM)', reports: 'Reports', maps: 'Maps', users: 'Users & Roles',
  audit: 'Audit', settings: 'System Settings', system: 'System Administration',
}

/** 'netflow.view' → 'NetFlow (view)'. */
export function permissionLabel(p: string): string {
  const [mod, action = ''] = p.split('.')
  const name = PERMISSION_MODULES[mod] ?? mod.replace(/_/g, ' ')
  return action ? `${name} (${action.replace(/_/g, ' ')})` : name
}
