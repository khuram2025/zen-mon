// Pure logic behind the Inventory settings form (Documentation/Inventory/03-UI-UX-SPEC.md section 3.9 and
// 05-P1-API-CONTRACT.md section 3.13): the draft the form edits, its inline validation, the diff that becomes the
// PUT body, and the words for the success toast. No React, no router, no API:
// scripts/test-inventory-software-settings.mjs exercises it directly.
//
// The server clamps out-of-range numbers instead of refusing them, so a saved value could differ from the one
// typed. The form therefore validates first (whole numbers inside the limits, fresh targets not above stale days
// x 24) and shows the limits as help text; what the server still adjusts is named in the toast.

import type {
  InventorySettings, NumericSettingKey, SettingsLimits, SettingsPatch, SettingsResponse,
} from '@/pages/inventory/softwareSettings'
import { categoryLabel, hiddenCategoryNames, hideSwitchLabel } from './softwareModel'

/** The fields of the form that hold a number. Text while they are edited: an empty or half-typed value is a state of its own. */
export const DRAFT_FIELDS = ['fresh_server', 'fresh_device', 'stale_days', 'change_retention_days', 'snapshot_hour'] as const
export type DraftField = (typeof DRAFT_FIELDS)[number]

export interface SettingsDraft {
  fresh_server: string
  fresh_device: string
  stale_days: string
  change_retention_days: string
  snapshot_hour: string
  /** Categories hidden by default. */
  hide: string[]
  trends_enabled: boolean
}

export function draftFromSettings(s: InventorySettings): SettingsDraft {
  return {
    fresh_server: String(s.fresh_hours.server),
    fresh_device: String(s.fresh_hours.device),
    stale_days: String(s.stale_days),
    change_retention_days: String(s.change_retention_days),
    snapshot_hour: String(s.snapshot_hour),
    hide: [...s.hide_categories_by_default],
    trends_enabled: s.trends_enabled,
  }
}

/** Which limit a field is checked against. */
const LIMIT_KEY: Record<DraftField, NumericSettingKey> = {
  fresh_server: 'fresh_hours',
  fresh_device: 'fresh_hours',
  stale_days: 'stale_days',
  change_retention_days: 'change_retention_days',
  snapshot_hour: 'snapshot_hour',
}

export const FIELD_LABELS: Record<DraftField, string> = {
  fresh_server: 'Servers are fresh for',
  fresh_device: 'Network devices are fresh for',
  stale_days: 'Stale after',
  change_retention_days: 'Keep change history for',
  snapshot_hour: 'Daily snapshot at',
}

export function limitFor(field: DraftField, limits: SettingsLimits): { min: number; max: number } {
  return limits[LIMIT_KEY[field]]
}

/** "1 day", "7 days": the number and its noun agree (the form used to say "Stale after 1 days"). */
const dayCount = (n: number) => `${n.toLocaleString('en-US')} ${n === 1 ? 'day' : 'days'}`

const range = (l: { min: number; max: number }) => `a whole number from ${l.min.toLocaleString('en-US')} to ${l.max.toLocaleString('en-US')}`

/** Reads a typed value: only whole numbers (digits, nothing else) count; `024` is 24. */
export function parseWhole(text: string): number | null {
  const t = text.trim()
  return /^\d{1,6}$/.test(t) ? Number(t) : null
}

/** A typed number in its canonical spelling (`024` becomes `24`, surrounding spaces go); anything that is not a whole number is left as typed. */
export function normaliseWhole(text: string): string {
  const n = parseWhole(text)
  return n === null ? text : String(n)
}

/** The draft with every numeric field in canonical spelling: what the form shows once a field is left (FE-12). */
export function normaliseDraft(draft: SettingsDraft): SettingsDraft {
  const next = { ...draft }
  for (const f of DRAFT_FIELDS) next[f] = normaliseWhole(draft[f])
  return next
}

export interface DraftCheck {
  /** One message per field that cannot be saved. */
  errors: Partial<Record<DraftField, string>>
  /** The settings the draft stands for; null while any field has an error. */
  values: InventorySettings | null
}

/** Inline validation: whole numbers inside the limits, and a fresh target never above stale days x 24 (what the server would lower it to). */
export function validateDraft(draft: SettingsDraft, limits: SettingsLimits): DraftCheck {
  const errors: Partial<Record<DraftField, string>> = {}
  const numbers: Partial<Record<DraftField, number>> = {}
  for (const field of DRAFT_FIELDS) {
    const limit = limitFor(field, limits)
    const raw = draft[field]
    if (raw.trim() === '') {
      errors[field] = `Enter ${range(limit)}.`
      continue
    }
    const n = parseWhole(raw)
    if (n === null) {
      errors[field] = `Enter ${range(limit)}; no decimals, signs or units.`
      continue
    }
    if (n < limit.min || n > limit.max) {
      errors[field] = `Must be ${range(limit)}.`
      continue
    }
    numbers[field] = n
  }
  const stale = numbers.stale_days
  if (stale !== undefined) {
    const cap = stale * 24
    for (const field of ['fresh_server', 'fresh_device'] as const) {
      const n = numbers[field]
      if (n !== undefined && n > cap) {
        errors[field] = `At most ${cap.toLocaleString('en-US')} hours: a fresh target cannot be longer than the stale days (${stale} x 24).`
        delete numbers[field]
      }
    }
  }
  if (Object.keys(errors).length > 0) return { errors, values: null }
  return {
    errors,
    values: {
      fresh_hours: { server: numbers.fresh_server!, device: numbers.fresh_device! },
      stale_days: numbers.stale_days!,
      change_retention_days: numbers.change_retention_days!,
      snapshot_hour: numbers.snapshot_hour!,
      hide_categories_by_default: [...draft.hide],
      trends_enabled: draft.trends_enabled,
    },
  }
}

const sameSet = (a: readonly string[], b: readonly string[]) => a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i])

/**
 * Only what changed: the PUT body. Keys left out keep their value on the server, and `fresh_hours` names only the
 * kind that changed, so two people editing different things do not overwrite each other.
 */
export function diffSettings(current: InventorySettings, next: InventorySettings): SettingsPatch {
  const patch: SettingsPatch = {}
  const fresh: NonNullable<SettingsPatch['fresh_hours']> = {}
  if (next.fresh_hours.server !== current.fresh_hours.server) fresh.server = next.fresh_hours.server
  if (next.fresh_hours.device !== current.fresh_hours.device) fresh.device = next.fresh_hours.device
  if (Object.keys(fresh).length) patch.fresh_hours = fresh
  if (next.stale_days !== current.stale_days) patch.stale_days = next.stale_days
  if (next.change_retention_days !== current.change_retention_days) patch.change_retention_days = next.change_retention_days
  if (next.snapshot_hour !== current.snapshot_hour) patch.snapshot_hour = next.snapshot_hour
  if (!sameSet(next.hide_categories_by_default, current.hide_categories_by_default)) patch.hide_categories_by_default = [...next.hide_categories_by_default]
  if (next.trends_enabled !== current.trends_enabled) patch.trends_enabled = next.trends_enabled
  return patch
}

export const isEmptyPatch = (p: SettingsPatch): boolean => Object.keys(p).length === 0

/**
 * Whether the draft differs from the settings in force. Numbers are compared as numbers (`024` is 24: the form must
 * not offer to save what is already in force, FE-12); a field that is not a whole number yet (empty, half typed) counts as a difference.
 */
export function draftDirty(draft: SettingsDraft, current: InventorySettings): boolean {
  const base = draftFromSettings(current)
  const changed = (f: DraftField) => {
    const n = parseWhole(draft[f])
    return n === null ? draft[f].trim() !== base[f] : n !== Number(base[f])
  }
  return DRAFT_FIELDS.some(changed) || !sameSet(draft.hide, base.hide) || draft.trends_enabled !== base.trends_enabled
}

/* ── Hidden categories ───────────────────────────────────────────────────── */

/** Checks or unchecks one category; the result keeps the order of the vocabulary and only holds known categories. */
export function toggleHidden(hide: readonly string[], category: string, hidden: boolean, vocabulary: readonly string[]): string[] {
  const set = new Set(hide)
  if (hidden) set.add(category)
  else set.delete(category)
  return vocabulary.filter((c) => set.has(c))
}

/** The sentence under the category list: what is hidden by default. The switch it names is the one the catalog shows. */
export function hiddenSummary(hide: readonly string[]): string {
  if (hide.length === 0) return 'Every category is listed. Nothing is hidden by default.'
  return `Hidden by default: ${hiddenCategoryNames(hide)}. They stay stored and counted. Switching off "${hideSwitchLabel(hide)}" in the catalog lists them, and a category chosen in its filter is never hidden.`
}

/* ── Help text ───────────────────────────────────────────────────────────── */

/** "1 to 720 hours. Default 24." */
export function helpFor(field: DraftField, limits: SettingsLimits, defaults: InventorySettings): string {
  const l = limitFor(field, limits)
  const unit = field === 'stale_days' || field === 'change_retention_days' ? 'days' : field === 'snapshot_hour' ? 'o\'clock, appliance time' : 'hours'
  const def: Record<DraftField, number> = {
    fresh_server: defaults.fresh_hours.server,
    fresh_device: defaults.fresh_hours.device,
    stale_days: defaults.stale_days,
    change_retention_days: defaults.change_retention_days,
    snapshot_hour: defaults.snapshot_hour,
  }
  return `${l.min.toLocaleString('en-US')} to ${l.max.toLocaleString('en-US')} ${unit}. Default ${def[field].toLocaleString('en-US')}.`
}

/** The freshness numbers as far as they can be read from the draft (a field that is not a number yet keeps its saved value). */
export function previewSettings(draft: SettingsDraft, current: InventorySettings): Pick<InventorySettings, 'fresh_hours' | 'stale_days'> {
  return {
    fresh_hours: {
      server: parseWhole(draft.fresh_server) ?? current.fresh_hours.server,
      device: parseWhole(draft.fresh_device) ?? current.fresh_hours.device,
    },
    stale_days: parseWhole(draft.stale_days) ?? current.stale_days,
  }
}

/** The freshness rule in one sentence, from the values being typed (a preview, not what is saved yet). */
export function freshnessPreview(values: Pick<InventorySettings, 'fresh_hours' | 'stale_days'>): string {
  const { server, device } = values.fresh_hours
  return `A server is Fresh for ${server.toLocaleString('en-US')} h after its last inventory, then Aging, and Stale after ${dayCount(values.stale_days)}. A network device is Fresh for ${device.toLocaleString('en-US')} h. An asset that never reported is Never.`
}

/* ── After saving ────────────────────────────────────────────────────────── */

const pad2 = (n: number) => String(n).padStart(2, '0')

/** What the keys that changed do, in words: the toast names the effect, not just "saved". */
export function effectLines(changed: readonly string[], saved: InventorySettings): string[] {
  const out: string[] = []
  const has = (k: string) => changed.includes(k)
  if (has('fresh_hours') || has('stale_days')) {
    out.push(`Freshness now counts servers as fresh within ${saved.fresh_hours.server} h, network devices within ${saved.fresh_hours.device} h, and anything older than ${dayCount(saved.stale_days)} as stale.`)
  }
  if (has('hide_categories_by_default')) {
    out.push(saved.hide_categories_by_default.length
      ? `The catalog and the Overview now leave out ${saved.hide_categories_by_default.map(categoryLabel).join(', ')}.`
      : 'The catalog and the Overview now include every category.')
  }
  if (has('change_retention_days')) out.push(`Change history older than ${dayCount(saved.change_retention_days)} is removed on the next cleanup pass.`)
  if (has('snapshot_hour')) out.push(`The daily snapshot is taken at ${pad2(saved.snapshot_hour)}:15 from the next run.`)
  if (has('trends_enabled')) out.push(saved.trends_enabled ? 'Trends are shown on the Overview again.' : 'Trends are hidden on the Overview; snapshots are still recorded.')
  return out
}

const ADJUSTED_NAMES: Record<string, string> = {
  stale_days: 'stale days',
  change_retention_days: 'change history retention',
  snapshot_hour: 'snapshot hour',
  'fresh_hours.server': 'the server fresh target',
  'fresh_hours.device': 'the network device fresh target',
  fresh_hours: 'a fresh target',
}

/** The numbers the appliance changed to stay inside its limits, if any. */
export function adjustedText(adjusted: readonly string[]): string | null {
  if (adjusted.length === 0) return null
  const names = adjusted.map((a) => ADJUSTED_NAMES[a] ?? a)
  return `The appliance adjusted ${names.join(', ')} to stay within its limits; the form shows what is in force.`
}

/** What a toast says after a PUT: the effect of each change, or that nothing changed. */
export function saveToast(resp: Pick<SettingsResponse, 'changed' | 'adjusted' | 'settings'>): { kind: 'success' | 'warning' | 'info'; title: string; description: string } {
  if (resp.changed.length === 0) {
    return { kind: 'info', title: 'Nothing to change', description: 'These values are already in force.' }
  }
  const lines = effectLines(resp.changed, resp.settings)
  const adjusted = adjustedText(resp.adjusted)
  if (adjusted) return { kind: 'warning', title: 'Settings saved with adjustments', description: [adjusted, ...lines].join(' ') }
  return { kind: 'success', title: 'Settings saved', description: lines.join(' ') }
}

/** Where the form is in its life, for the status line next to the buttons. */
export function formStatus(options: { dirty: boolean; invalid: boolean; saving: boolean }): string {
  if (options.saving) return 'Saving'
  if (options.invalid) return 'Fix the highlighted fields to save'
  if (options.dirty) return 'Unsaved changes'
  return 'All changes saved'
}
