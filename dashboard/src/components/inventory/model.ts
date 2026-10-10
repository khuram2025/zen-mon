// Pure logic behind the Inventory primitives (components/inventory/*). No React, no router, no
// API: scripts/test-inventory-ui.mjs exercises it directly, and the components only render what
// it returns.

import { cleanTags } from '@/components/access/roleModel'
import type { Freshness } from '@/pages/inventory/filters'

/* ── Numbers ─────────────────────────────────────────────────────────────── */

const INT = new Intl.NumberFormat('en-US')

/**
 * An exact count with thousands separators. Inventory counts are never abbreviated ("1.8K"):
 * every number is a link to the list behind it, and that list's total must read the same
 * (doc 03 section 2, drill-down contract). Missing is a dash, never a zero.
 */
export function fmtInt(n: number | null | undefined): string {
  return typeof n === 'number' && Number.isFinite(n) ? INT.format(Math.round(n)) : '—'
}

/**
 * A percentage to one decimal that never rounds into a claim it cannot keep: "100.0%" only when it IS 100 (every
 * one is counted) and "0.0%" only when it is 0 (none is). 99.96 reads "99.9%" and 0.04 reads "<0.1%", so a
 * line such as "(100.0%) · 1 without" or a slice printed 0.0% next to a link that opens one asset cannot occur
 * (review findings NF-07, NF-FE-1). Missing is a dash, never 0%. Every share printed in Inventory goes through
 * this function or {@link fmtShare}, never `toFixed(1)`.
 */
export function fmtPct(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  if (n >= 100) return '100.0%'
  if (n <= 0) return '0.0%'
  if (n < 0.05) return '<0.1%'
  return `${Math.min(99.9, Math.round(n * 10) / 10).toFixed(1)}%`
}

/** {@link fmtPct} in words for a screen reader: "99.9 percent", "<0.1 percent". */
export function fmtPctWords(n: number | null | undefined): string {
  const text = fmtPct(n)
  return text.endsWith('%') ? `${text.slice(0, -1)} percent` : text
}

/**
 * `count` of `total` as a percentage with one decimal (see {@link fmtPct}). `total <= 0` has no percentage: a dash,
 * never 0%.
 */
export function fmtShare(count: number, total: number): string {
  if (!Number.isFinite(count) || !Number.isFinite(total) || total <= 0) return '—'
  if (count >= total) return '100.0%'
  if (count <= 0) return '0.0%'
  return fmtPct((count / total) * 100)
}

/** `count / total` as a percentage, 0 when there is nothing to divide by. */
export function share(count: number, total: number): number {
  return total > 0 && count > 0 ? (count / total) * 100 : 0
}

export function plural(n: number, noun: string, many = `${noun}s`): string {
  return `${fmtInt(n)} ${n === 1 ? noun : many}`
}

/* ── Freshness ───────────────────────────────────────────────────────────── */

export const FRESHNESS_STATES: readonly Freshness[] = ['fresh', 'aging', 'stale', 'never', 'not_applicable']

/** Mirrors `system_settings['inventory']` (doc 02 section 9). */
export interface FreshnessThresholds {
  /** Hours since the last inventory within which an asset is Fresh, per kind. */
  fresh_hours: { server: number; device: number }
  /** Days after which it is Stale; between the two it is Aging. */
  stale_days: number
}

export const DEFAULT_THRESHOLDS: FreshnessThresholds = { fresh_hours: { server: 24, device: 2 }, stale_days: 7 }

const HOUR = 3_600_000
const DAY = 24 * HOUR

export function toMillis(iso: string | null | undefined): number | null {
  if (!iso) return null
  // Backend timestamps are UTC; a value without an offset is read as UTC, as lib/utils relativeTime does.
  const s = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso.replace(' ', 'T')}Z`
  const t = Date.parse(s)
  return Number.isFinite(t) ? t : null
}

/**
 * Where an asset stands, from the time of its last inventory. Never = no inventory timestamp at all.
 * The server's `freshness` field is authoritative; this is the same rule for places that only hold a
 * timestamp (and for the settings preview). A timestamp in the future is treated as Fresh (clock skew).
 */
export function classifyFreshness(
  kind: 'server' | 'device',
  inventoryAt: string | null | undefined,
  now: number,
  thresholds: FreshnessThresholds = DEFAULT_THRESHOLDS,
): Freshness {
  const at = toMillis(inventoryAt)
  if (at === null) return 'never'
  const age = now - at
  if (age <= thresholds.fresh_hours[kind] * HOUR) return 'fresh'
  if (age <= thresholds.stale_days * DAY) return 'aging'
  return 'stale'
}

/** "just now", "5m ago", "6h ago", "3d ago", "never": the wording lib/utils relativeTime uses, with an injectable clock. */
export function ageWords(iso: string | null | undefined, now: number): string {
  const at = toMillis(iso)
  if (at === null) return iso ? '—' : 'never'
  const diff = (now - at) / 1000
  if (diff < 10) return 'just now'
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

export interface FreshnessMeta {
  label: string
  /** Pill colours: status foreground tokens only (contrast-checked), never the raw status colours. */
  tone: string
  /** Dot beside the label; the label is always there too, so colour is never the only signal. */
  dot: string
  meaning: string
}

export const FRESHNESS_META: Record<Freshness, FreshnessMeta> = {
  fresh: { label: 'Fresh', tone: 'border-success/30 bg-success/10 text-success-fg', dot: 'bg-success', meaning: 'Inventory reported within its freshness target' },
  aging: { label: 'Aging', tone: 'border-warning/35 bg-warning/10 text-warning-fg', dot: 'bg-warning', meaning: 'Inventory is older than its freshness target' },
  stale: { label: 'Stale', tone: 'border-danger/30 bg-danger/10 text-danger-fg', dot: 'bg-danger', meaning: 'Inventory has not been refreshed for a long time' },
  // Never is the quiet state, but its label still needs AA contrast on the pill's own surface2 fill:
  // muted text on surface2 is 4.3:1 in the light theme, so the text is text2 and only the dot is muted.
  never: { label: 'Never', tone: 'border-border bg-surface2 text-text2', dot: 'bg-muted/60', meaning: 'This asset can report an inventory but never has' },
  // Not a gap: the asset cannot send an inventory by design. A hollow dot and dashed border keep it apart from "Never" without colour.
  not_applicable: {
    label: 'Not reported by design',
    tone: 'border-dashed border-border bg-transparent text-text2',
    dot: 'border border-muted bg-transparent',
    meaning: 'This asset cannot send an inventory by design (a ping-only or controller-managed device, or a server whose collection mode has no inventory upload), so it is not counted as never reported',
  },
}

/** Tooltip that keeps reachability and data apart (doc 03 section 2). */
export function freshnessTitle(
  freshness: Freshness,
  inventoryAt: string | null | undefined,
  lastSeen: string | null | undefined,
  now: number,
): string {
  const meta = FRESHNESS_META[freshness]
  const seen = `Last seen (reachability): ${lastSeen ? ageWords(lastSeen, now) : 'never'}.`
  // An asset that cannot report has no "last inventory" to be late with: saying "never" would read as a gap.
  if (freshness === 'not_applicable') return `${meta.label}: ${meta.meaning}. ${seen}`
  return [`${meta.label}: ${meta.meaning}.`, `Last inventory (data): ${inventoryAt ? ageWords(inventoryAt, now) : 'never'}.`, seen].join(' ')
}

/* ── Sources ─────────────────────────────────────────────────────────────── */

/** Where an identity value came from (doc 03 section 2, doc 02 `os_source`). */
export const SOURCE_LABELS: Record<string, string> = {
  agent: 'Agent',
  snmp: 'SNMP',
  profile: 'SNMP profile',
  template: 'Template',
  template_stale: 'Template (old)',
  entity: 'ENTITY-MIB',
  sysdescr: 'sysDescr',
  managed: 'Managed',
  winrm: 'WinRM',
  ssh: 'SSH',
  wmi: 'WMI',
  controller: 'Controller',
  icmp: 'ICMP',
}

/**
 * A source slug as words: the known labels (identity sources AND install sources, see {@link INSTALL_SOURCE_LABELS}),
 * otherwise the slug with underscores as spaces ("some_thing" becomes "Some thing"; one of four letters or fewer
 * is an acronym and is capitalised: "rpm" stays readable as "RPM"). Never the raw slug ("Hklm_wow6432").
 */
export function sourceLabel(source: string | null | undefined): string {
  const key = (source ?? '').trim().toLowerCase()
  if (!key) return 'Unknown source'
  const known = SOURCE_LABELS[key] ?? INSTALL_SOURCE_LABELS[key]
  if (known) return known
  const words = key.replace(/_+/g, ' ')
  return words.length <= 4 ? words.toUpperCase() : words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * Where an INSTALLATION was found on the machine (`server_software_inventory.source`), as the agent spells it.
 * The 32-bit Uninstall hive of a 64-bit Windows holds every 32-bit program, so `hklm_wow6432` is common.
 */
export const INSTALL_SOURCE_LABELS: Record<string, string> = {
  hklm: 'HKLM',
  hklm_wow6432: 'HKLM (32-bit)',
  hkcu: 'HKCU (current user)',
  dpkg: 'dpkg',
  rpm: 'rpm',
  msi: 'MSI',
  appx: 'AppX',
  pkg: 'pkg',
  snap: 'snap',
  brew: 'Homebrew',
}

/** The label of where an installation was found ("HKLM (32-bit)" for `hklm_wow6432`); the same words as {@link sourceLabel}, with "Unknown" for none. */
export function installSourceLabel(source: string | null | undefined): string {
  return (source ?? '').trim() ? sourceLabel(source) : 'Unknown'
}

/* ── Versions ────────────────────────────────────────────────────────────── */

/**
 * Natural order of two version strings: digit runs compare as numbers, letter runs as text, and
 * with an equal prefix the longer one is newer ("17.9.4" < "17.9.4a", "11.1.2-h3" < "11.1.2-h10",
 * "10.0.20348.587" < "10.0.20348.2700"). A DISPLAY fallback for bars and lists that have no server
 * rank. It never decides "older than" for a policy or an alert: only the server's scheme-aware
 * comparators (services/versioning.py) may, because they also refuse to compare across trains.
 */
export function naturalVersionCompare(a: string, b: string): number {
  const ta = a.match(/\d+|[A-Za-z]+/g) ?? []
  const tb = b.match(/\d+|[A-Za-z]+/g) ?? []
  const n = Math.min(ta.length, tb.length)
  for (let i = 0; i < n; i++) {
    const x = ta[i]
    const y = tb[i]
    const xn = /^\d/.test(x)
    const yn = /^\d/.test(y)
    if (xn && yn) {
      const d = Number(x) - Number(y)
      if (d !== 0) return d < 0 ? -1 : 1
    } else if (xn !== yn) {
      return xn ? 1 : -1
    } else {
      const c = x.toLowerCase().localeCompare(y.toLowerCase())
      if (c !== 0) return c < 0 ? -1 : 1
    }
  }
  if (ta.length !== tb.length) return ta.length < tb.length ? -1 : 1
  return 0
}

export interface VersionEntry {
  /** Stable identity (usually the version string). */
  key: string
  /** What the segment is called. */
  label: string
  count: number
  /** Server comparator rank (`cmp_rank`): higher is newer. When every entry has one it decides the order. */
  rank?: number | null
  /** Where a click goes: the list behind this number. */
  href?: string
  /** An unknown version is a value of its own: shown, counted, never folded into "other". */
  unknown?: boolean
}

/** Newest first. The server's rank wins when all entries carry one; otherwise natural order of the labels. */
export function orderVersionEntries<T extends VersionEntry>(entries: readonly T[]): T[] {
  const ranked = entries.length > 0 && entries.every((e) => typeof e.rank === 'number')
  return [...entries].sort((a, b) => {
    const d = ranked ? (b.rank as number) - (a.rank as number) : naturalVersionCompare(b.label, a.label)
    return d !== 0 ? d : a.label.localeCompare(b.label)
  })
}

export interface VersionSegment {
  kind: 'version' | 'other' | 'unknown'
  key: string
  label: string
  count: number
  /** Share of the whole bar, 0 to 100. */
  pct: number
  href?: string
  /** For `other`: how many distinct versions it stands for. */
  versions?: number
  /** Position among the version segments, 0 = newest (drives the colour ramp). */
  index: number
}

export interface VersionBuckets {
  segments: VersionSegment[]
  total: number
  /** Distinct known versions before collapsing. */
  distinct: number
  /** The versions folded into `other`, newest first. */
  folded: VersionEntry[]
}

export const DEFAULT_MAX_SEGMENTS = 6

/**
 * Turns per-version counts into bar segments. Known versions come newest first. When there are more
 * than `maxSegments`, the largest groups (by count) keep their own segment and the rest collapse into
 * one trailing "other" segment; the unknown version is never collapsed and always comes last, so it
 * stays visible, counted and drillable.
 */
export function collapseVersionBuckets(
  entries: readonly VersionEntry[],
  options: {
    maxSegments?: number
    otherHref?: string
    otherLabel?: (versions: number) => string
    /**
     * What the shares are shares OF. The default is the sum of the entries, which is right only when each
     * thing is counted once (a device runs ONE firmware version). When a host can appear in several entries
     * (it runs two versions of a product), that sum is larger than the number of hosts and every share would
     * be too small: pass the number of distinct hosts. (Review finding (k); the software pages use their own
     * SoftwareVersionBar for the same reason.)
     */
    total?: number
  } = {},
): VersionBuckets {
  const max = Math.max(2, options.maxSegments ?? DEFAULT_MAX_SEGMENTS)
  const live = entries.filter((e) => e.count > 0)
  const total = options.total !== undefined ? Math.max(0, options.total) : live.reduce((s, e) => s + e.count, 0)
  const known = orderVersionEntries(live.filter((e) => !e.unknown))
  const unknown = live.filter((e) => e.unknown)

  let shown = known
  let folded: VersionEntry[] = []
  if (known.length > max) {
    const keep = new Set(
      [...known]
        .sort((a, b) => b.count - a.count || known.indexOf(a) - known.indexOf(b))
        .slice(0, max - 1)
        .map((e) => e.key),
    )
    shown = known.filter((e) => keep.has(e.key))
    folded = known.filter((e) => !keep.has(e.key))
  }

  const segments: VersionSegment[] = shown.map((e, index) => ({
    kind: 'version', key: e.key, label: e.label, count: e.count, pct: share(e.count, total), href: e.href, index,
  }))
  if (folded.length > 0) {
    const count = folded.reduce((s, e) => s + e.count, 0)
    segments.push({
      kind: 'other',
      key: '__other__',
      label: options.otherLabel ? options.otherLabel(folded.length) : `${folded.length} other versions`,
      count,
      pct: share(count, total),
      href: options.otherHref,
      versions: folded.length,
      index: shown.length,
    })
  }
  const unknownCount = unknown.reduce((s, e) => s + e.count, 0)
  if (unknownCount > 0) {
    segments.push({
      kind: 'unknown',
      key: '__unknown__',
      label: 'Version unknown',
      count: unknownCount,
      pct: share(unknownCount, total),
      href: unknown.find((e) => e.href)?.href,
      index: shown.length + (folded.length ? 1 : 0),
    })
  }
  return { segments, total, distinct: known.length, folded }
}

/** Tooltip and screen-reader text of one segment, for example "152.0.8: 41 hosts (34.2%)". */
export function segmentText(s: VersionSegment, noun = 'host'): string {
  return `${s.label}: ${plural(s.count, noun)} (${fmtPct(s.pct)})`
}

/** One-line summary of a whole bar, for its accessible name. */
export function barSummary(b: VersionBuckets, noun = 'host'): string {
  if (b.total === 0) return 'No installations'
  const first = b.segments.find((s) => s.kind === 'version')
  const distinct = `${plural(b.distinct, 'version')}`
  return first ? `${distinct}; newest ${first.label} on ${plural(first.count, noun)}; ${plural(b.total, noun)} in total` : `${plural(b.total, noun)} in total`
}

/* ── Facets ──────────────────────────────────────────────────────────────── */

export interface FacetItem {
  /** The value that goes into the filter. */
  value: string
  label?: string
  count: number
  /** "Unknown" is a value: muted, counted, always listed last, drillable. */
  unknown?: boolean
}

/** Largest first, ties by label; the unknown item goes last whatever its count. */
export function sortFacetItems<T extends FacetItem>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => {
    if (!!a.unknown !== !!b.unknown) return a.unknown ? 1 : -1
    return b.count - a.count || (a.label ?? a.value).localeCompare(b.label ?? b.value, undefined, { numeric: true })
  })
}

/**
 * The items to list now: selected values always stay visible (even at count 0 or beyond the limit),
 * a text filter narrows the rest, and `expanded` lifts the limit.
 */
export function visibleFacetItems<T extends FacetItem>(
  items: readonly T[],
  selected: readonly string[],
  options: { limit?: number; expanded?: boolean; filter?: string } = {},
): { shown: T[]; hidden: number } {
  const limit = options.limit ?? 8
  const needle = (options.filter ?? '').trim().toLowerCase()
  const sel = new Set(selected.map((s) => s.toLowerCase()))
  const matches = items.filter((i) => !needle || (i.label ?? i.value).toLowerCase().includes(needle) || i.value.toLowerCase().includes(needle))
  if (options.expanded || needle || matches.length <= limit) return { shown: [...matches], hidden: 0 }
  const head = matches.slice(0, limit)
  const rest = matches.slice(limit)
  const pinned = rest.filter((i) => sel.has(i.value.toLowerCase()))
  const shown = [...head, ...pinned]
  return { shown, hidden: matches.length - shown.length }
}

/* ── Meta-line ───────────────────────────────────────────────────────────── */

/** "14:05" for today, otherwise "2026-10-07 14:05" in the browser's time zone. */
export function asOfLabel(iso: string | null | undefined, now: number): string {
  const at = toMillis(iso)
  if (at === null) return 'unknown'
  const d = new Date(at)
  const n = new Date(now)
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()
  if (sameDay) return hhmm
  const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return `${ymd} ${hhmm}`
}

/** A user's effective visibility scope: the tags on the account and the tags inherited from the role, together. */
export function effectiveScopeTags(user: { scope_tags?: readonly string[]; role_scope_tags?: readonly string[] } | null | undefined): string[] {
  return cleanTags([...(user?.scope_tags ?? []), ...(user?.role_scope_tags ?? [])])
}

/** Facet items as the API returns them (`{value, label, count}`), with the `unknown` sentinel marked as the unknown row. */
export function facetItemsFromApi(items: readonly { value: string; label?: string; count: number }[] | null | undefined): FacetItem[] {
  return (items ?? []).map((i) => ({ value: i.value, label: i.label, count: i.count, unknown: i.value.toLowerCase() === 'unknown' || undefined }))
}
