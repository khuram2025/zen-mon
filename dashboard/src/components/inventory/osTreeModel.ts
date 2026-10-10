// Pure logic behind the Operating Systems page (Documentation/Inventory/03-UI-UX-SPEC.md section 3.3):
// the family > product > release > version tree, its sorting and expansion, the family donut and its
// legend, and the build-currency rows. No React, no router, no API: scripts/test-inventory-os-hardware-changes.mjs
// exercises it directly, and the page only renders what it returns.
//
// Principle 1: every count is a link to the assets behind it, and the drilled list's total EQUALS the
// number clicked. The filters of a node are the path to it (05-P1-API-CONTRACT.md section 4): os_family,
// os_product, os_release, os_version, with `unknown` for a missing level, plus `kind` when the tree is
// filtered. `currency.older` and `currency.unknown` are not list totals and are plain text.

import type { AssetFilters, AssetKind } from '@/pages/inventory/filters'
import type { OsCurrency, OsFamilyNode, OsProductNode, OsReleaseNode, OsResponse, OsVersionNode } from '@/pages/inventory/types'
import { fmtPct, fmtPctWords, share } from './model'
import { PATHS, assetsDrill, pageDrill, releaseFilters, releaseLabel, type Drill } from './overviewModel'

/* ── The kind switch (?kind=) ────────────────────────────────────────────── */

export type OsKindChoice = 'all' | AssetKind

export const OS_KIND_OPTIONS: readonly { value: OsKindChoice; label: string; noun: string }[] = [
  { value: 'all', label: 'All', noun: 'assets' },
  { value: 'server', label: 'Servers', noun: 'servers' },
  { value: 'device', label: 'Network', noun: 'network devices' },
]

/** `?kind=` read from the address: only `server` and `device` count; anything else is "All" and is reported. */
export function parseOsKind(params: URLSearchParams): { kind: OsKindChoice; rejected: string | null } {
  const raw = params.get('kind')
  if (raw === null || raw === '') return { kind: 'all', rejected: null }
  const v = raw.trim().toLowerCase()
  if (v === 'server' || v === 'device') return { kind: v, rejected: null }
  return { kind: 'all', rejected: `kind=${raw} (expected server or device)` }
}

/** The API's `kind` parameter for a choice (`all` sends none). */
export const osKindParam = (kind: OsKindChoice): AssetKind | undefined => (kind === 'all' ? undefined : kind)

/** The same URL with another kind: `all` removes the parameter, every other parameter stays. */
export function withOsKind(params: URLSearchParams, kind: OsKindChoice): URLSearchParams {
  const next = new URLSearchParams(params)
  if (kind === 'all') next.delete('kind')
  else next.set('kind', kind)
  return next
}

/* ── Sorting (?sort=&order=) ─────────────────────────────────────────────── */

/** `null` is the API's own order: families by count, releases newest first, versions newest first. */
export type OsSortKey = 'name' | 'count' | 'versions'
export type OsSortOrder = 'asc' | 'desc'
export interface OsSort {
  key: OsSortKey | null
  order: OsSortOrder
}
export const OS_SORT_KEYS: readonly OsSortKey[] = ['name', 'count', 'versions']
export const DEFAULT_OS_SORT: OsSort = { key: null, order: 'desc' }

/** Counts start with the biggest, names with A to Z. */
const NATURAL_ORDER: Record<OsSortKey, OsSortOrder> = { name: 'asc', count: 'desc', versions: 'desc' }

export function parseOsSort(params: URLSearchParams): OsSort {
  const key = params.get('sort')
  if (!key || !(OS_SORT_KEYS as readonly string[]).includes(key)) return DEFAULT_OS_SORT
  const order = params.get('order')
  return { key: key as OsSortKey, order: order === 'asc' || order === 'desc' ? order : NATURAL_ORDER[key as OsSortKey] }
}

/**
 * Clicking a column header: a new column starts in its natural direction, the same column flips, and a
 * third click goes back to the API's order (so the unsorted view is always one click away).
 */
export function nextOsSort(current: OsSort, key: OsSortKey): OsSort {
  if (current.key !== key) return { key, order: NATURAL_ORDER[key] }
  if (current.order === NATURAL_ORDER[key]) return { key, order: current.order === 'asc' ? 'desc' : 'asc' }
  return DEFAULT_OS_SORT
}

export function withOsSort(params: URLSearchParams, sort: OsSort): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete('sort')
  next.delete('order')
  if (sort.key) {
    next.set('sort', sort.key)
    next.set('order', sort.order)
  }
  return next
}

/* ── The tree ────────────────────────────────────────────────────────────── */

export type OsRowType = 'family' | 'product' | 'release' | 'version'

export interface OsTreeRow {
  /** Stable identity, also what the expansion state is keyed by. */
  key: string
  type: OsRowType
  /** Depth under the first column: 0 family, 1 product, 2 release, 3 version (a family without a product row skips level 1). */
  depth: number
  parent: string | null
  label: string
  /**
   * The row's name with its place in the tree, for a screen reader and a tooltip (review finding UI-13): "Windows (family)",
   * "Windows Server (product)", "Windows Server 2022", "Windows Server 2022 version 10.0.20348.2527". Release and version
   * labels repeat across the tree ("22H2", "7.4"); the path never does.
   */
  path: string
  /** The name is the word "Unknown" (a family, product, release or version nobody could identify). */
  unknown: boolean
  count: number
  /** Share of every asset in the tree, 0 to 100. */
  pct: number
  /** Distinct known versions below the node (null on a version row). */
  versions: number | null
  /** Releases only: the highest observed version, comparator-ordered by the server. */
  latest: string | null
  /** Versions only: one raw string the normalised version came from (network firmware). */
  raw: string | null
  /** The row has rows beneath it, so it can be expanded. */
  expandable: boolean
  /** The assets behind `count`: the drilled list's total equals it. */
  drill: Drill
  /** Release rows: the node, for the currency card and for tests. */
  release?: OsReleaseNode
}

const isUnknownFamily = (f: OsFamilyNode) => f.family === 'unknown'

/**
 * A family with ONE product that is called like the family (FortiOS under FortiOS), or has no product at all
 * (Unknown, Other), gains nothing from a product row repeating it: its releases sit directly under the family.
 */
function showsProductRow(family: OsFamilyNode, product: OsProductNode): boolean {
  if (family.products.length !== 1) return true
  if (product.product === null) return false
  return product.product.trim().toLowerCase() !== family.label.trim().toLowerCase()
}

/** Distinct known (release, version) pairs below a node. */
function versionPairs(releases: readonly OsReleaseNode[]): number {
  const seen = new Set<string>()
  for (const r of releases) for (const v of r.versions) if (v.version !== null) seen.add(`${r.release ?? ''}|${v.version}`)
  return seen.size
}

const versionDrill = (kind: AssetKind | undefined, f: OsFamilyNode, p: OsProductNode, r: OsReleaseNode, v: OsVersionNode): Drill =>
  assetsDrill({ ...releaseFilters(kind, f, p, r), os_version: v.version ?? 'unknown' }, v.count)

const compareLabels = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })

/** Orders siblings: the unknown node always last, whatever the key; ties keep the API's order. */
function ordered<T>(items: readonly T[], sort: OsSort, pick: { name: (t: T) => string; count: (t: T) => number; versions: (t: T) => number; unknown: (t: T) => boolean }): T[] {
  if (!sort.key) return [...items]
  const dir = sort.order === 'asc' ? 1 : -1
  const index = new Map(items.map((t, i) => [t, i]))
  return [...items].sort((a, b) => {
    if (pick.unknown(a) !== pick.unknown(b)) return pick.unknown(a) ? 1 : -1
    let d = 0
    if (sort.key === 'name') d = compareLabels(pick.name(a), pick.name(b))
    else if (sort.key === 'count') d = pick.count(a) - pick.count(b)
    else d = pick.versions(a) - pick.versions(b)
    if (d !== 0) return d * dir
    return (index.get(a) ?? 0) - (index.get(b) ?? 0)
  })
}

/**
 * Every row of the tree, in display order, expanded or not (use {@link visibleOsRows} to apply the
 * expansion). `kind` is the page's kind switch: it is part of every drill, so a node's count equals the
 * list it opens.
 */
export function buildOsRows(os: OsResponse, kind: OsKindChoice = 'all', sort: OsSort = DEFAULT_OS_SORT): OsTreeRow[] {
  const assetKind = osKindParam(kind)
  const total = os.total
  const rows: OsTreeRow[] = []

  const families = ordered(os.families, sort, {
    name: (f) => f.label,
    count: (f) => f.count,
    versions: (f) => versionPairs(f.products.flatMap((p) => p.releases)),
    unknown: isUnknownFamily,
  })

  for (const f of families) {
    const fKey = `f:${f.family}`
    const fUnknown = isUnknownFamily(f)
    rows.push({
      key: fKey, type: 'family', depth: 0, parent: null, label: fUnknown ? 'Unknown' : f.label,
      path: fUnknown ? 'Unknown operating system' : `${f.label} (family)`, unknown: fUnknown,
      count: f.count, pct: share(f.count, total),
      versions: fUnknown ? 0 : versionPairs(f.products.flatMap((p) => p.releases)),
      // The unknown family is one row: its only release would repeat it.
      latest: null, raw: null, expandable: !fUnknown && f.products.length > 0,
      drill: assetsDrill({ ...(assetKind ? { kind: assetKind } : {}), os_family: f.family }, f.count),
    })

    const products = ordered(f.products, sort, {
      name: (p) => p.product ?? '',
      count: (p) => p.count,
      versions: (p) => versionPairs(p.releases),
      // A product-less node of a known family goes last, like the API does.
      unknown: (p) => p.product === null,
    })
    for (const p of products) {
      const withRow = showsProductRow(f, p)
      const pKey = `p:${f.family}/${p.product ?? ''}`
      const releaseParent = withRow ? pKey : fKey
      const releaseDepth = withRow ? 2 : 1
      if (withRow) {
        const pUnknown = p.product === null
        rows.push({
          key: pKey, type: 'product', depth: 1, parent: fKey,
          label: pUnknown ? (fUnknown ? 'Unknown' : `${f.label} (product unknown)`) : (p.product as string),
          path: pUnknown ? (fUnknown ? 'Unknown operating system' : `${f.label} (product unknown)`) : `${p.product} (product of ${f.label})`, unknown: pUnknown && fUnknown,
          count: p.count, pct: share(p.count, total), versions: versionPairs(p.releases), latest: null, raw: null,
          expandable: p.releases.length > 0,
          drill: assetsDrill({ ...(assetKind ? { kind: assetKind } : {}), os_family: f.family, os_product: p.product ?? 'unknown' }, p.count),
        })
      }

      const releases = ordered(p.releases, sort, {
        name: (r) => r.release ?? '',
        count: (r) => r.count,
        versions: (r) => r.distinct_versions,
        unknown: (r) => r.release === null,
      })
      for (const r of releases) {
        const rKey = `r:${f.family}/${p.product ?? ''}/${r.release ?? ''}`
        const rUnknown = r.release === null
        // A release whose only "version" is the unknown one has nothing to expand: that row would say the same thing.
        const onlyUnknown = r.versions.every((v) => v.version === null)
        rows.push({
          key: rKey, type: 'release', depth: releaseDepth, parent: releaseParent,
          label: rUnknown ? (fUnknown ? 'Unknown' : 'Release unknown') : (r.release as string),
          path: fUnknown ? 'Unknown operating system' : releaseLabel(f, p, r), unknown: rUnknown, count: r.count, pct: share(r.count, total), versions: r.distinct_versions,
          latest: r.latest_version, raw: null, expandable: !onlyUnknown && r.versions.length > 0,
          drill: assetsDrill(releaseFilters(assetKind, f, p, r), r.count), release: r,
        })
        // Versions follow the name and count sorts; the API's newest-first order stays for the versions column.
        const versions = sort.key === 'versions'
          ? r.versions
          : ordered(r.versions, sort, { name: (v) => v.version ?? '', count: (v) => v.count, versions: () => 0, unknown: (v) => v.version === null })
        for (const v of versions) {
          rows.push({
            key: `v:${f.family}/${p.product ?? ''}/${r.release ?? ''}/${v.version ?? ''}`, type: 'version', depth: releaseDepth + 1, parent: rKey,
            label: v.version ?? 'Version unknown',
            path: `${fUnknown ? 'Unknown operating system' : releaseLabel(f, p, r)}${v.version === null ? ', version unknown' : ` version ${v.version}`}`,
            unknown: v.version === null, count: v.count, pct: share(v.count, total),
            versions: null, latest: null, raw: v.raw_example, expandable: false,
            drill: versionDrill(assetKind, f, p, r, v),
          })
        }
      }
    }
  }
  return rows
}

/** Which rows are open at first: every family and product, no release (so the page opens on the releases, versions one click away). */
export function defaultExpanded(rows: readonly OsTreeRow[]): Set<string> {
  return new Set(rows.filter((r) => r.expandable && (r.type === 'family' || r.type === 'product')).map((r) => r.key))
}

/** The rows to draw: a row is shown when every ancestor is expanded. */
export function visibleOsRows(rows: readonly OsTreeRow[], expanded: ReadonlySet<string>): OsTreeRow[] {
  const byKey = new Map(rows.map((r) => [r.key, r]))
  const shown = (r: OsTreeRow): boolean => {
    let p = r.parent
    while (p) {
      if (!expanded.has(p)) return false
      p = byKey.get(p)?.parent ?? null
    }
    return true
  }
  return rows.filter(shown)
}

/** Every expandable row's key, for "Expand all". */
export const allExpandable = (rows: readonly OsTreeRow[]): Set<string> => new Set(rows.filter((r) => r.expandable).map((r) => r.key))

/** Toggles one row; a collapsed parent keeps its children's own state for when it opens again. */
export function toggleExpanded(expanded: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(expanded)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}

/** How many versions the row stands for, in words: "3 versions", "1 version", "no version" (never a zero that reads as healthy). */
export function versionsText(row: Pick<OsTreeRow, 'versions' | 'type'>): string {
  if (row.versions === null) return ''
  if (row.versions === 0) return 'none identified'
  return `${row.versions} ${row.versions === 1 ? 'version' : 'versions'}`
}

/** What the row's expand button announces ("Expand Windows Server 2022, 3 versions", "Collapse Windows (family)"): unique in the tree. */
export function expandLabel(row: Pick<OsTreeRow, 'path' | 'versions' | 'type'>, open: boolean): string {
  const what = row.type === 'release' ? versionsText(row) : ''
  return `${open ? 'Collapse' : 'Expand'} ${row.path}${what ? `, ${what}` : ''}`
}

/** "1 server" / "2 servers": the plural noun of the kind switch, singular for one. */
export const nounFor = (count: number, noun: string): string => (count === 1 ? noun.replace(/s$/, '') : noun)

/** The row's text for a screen reader: "Windows Server 2022: 18 assets, 12.4 percent of 145" (the path, not the bare label). */
export function rowSummary(row: Pick<OsTreeRow, 'path' | 'count' | 'pct'>, total: number, noun = 'assets'): string {
  return `${row.path}: ${row.count} ${nounFor(row.count, noun)}, ${fmtPctWords(row.pct)} of ${total}`
}

/* ── The donut ───────────────────────────────────────────────────────────── */

/** CATEGORICAL has five colours: the five biggest families get one; the rest are one neutral slice. */
export const MAX_DONUT_FAMILIES = 5

export interface FamilySlice {
  key: string
  label: string
  count: number
  pct: number
  color?: number
  unknown?: boolean
  other?: boolean
  drill: Drill
}

export interface FamilyLegendRow {
  key: string
  label: string
  count: number
  pct: number
  /** Index into CATEGORICAL for the five biggest families. */
  color?: number
  unknown?: boolean
  /** A family folded into the grey "Other" slice: the legend lists it under its group row. */
  tail?: boolean
  drill: Drill
}

export interface FamilyDonutModel {
  total: number
  noun: string
  slices: FamilySlice[]
  legend: FamilyLegendRow[]
  /** How many families the neutral slice folds together (0: none). */
  folded: number
  /** The grey slice itself ("Other (6 families)"): the legend shows it as the heading of the families it folds. */
  other: FamilySlice | null
}

/** Donut slices (families) and a legend that lists EVERY family, linked, so folding never hides one. */
export function familyDonut(os: OsResponse, kind: OsKindChoice = 'all'): FamilyDonutModel {
  const assetKind = osKindParam(kind)
  const total = os.total
  const known = os.families.filter((f) => !isUnknownFamily(f) && f.count > 0).sort((a, b) => b.count - a.count || compareLabels(a.label, b.label))
  const unknown = os.families.find((f) => isUnknownFamily(f) && f.count > 0)
  // Five colours: five families get one each. One family over the palette would be a lone "Other (1 family)" (or, worse, a
  // sixth family sharing the first one's colour), so then the two smallest fold together.
  const keep = known.length <= MAX_DONUT_FAMILIES ? known : known.length === MAX_DONUT_FAMILIES + 1 ? known.slice(0, MAX_DONUT_FAMILIES - 1) : known.slice(0, MAX_DONUT_FAMILIES)
  const folded = known.slice(keep.length)
  const colour = new Map(keep.map((f, i) => [f.family, i]))
  const familyDrill = (f: OsFamilyNode) => assetsDrill({ ...(assetKind ? { kind: assetKind } : {}), os_family: f.family }, f.count)

  const slices: FamilySlice[] = keep.map((f, i) => ({ key: f.family, label: f.label, count: f.count, pct: share(f.count, total), color: i, drill: familyDrill(f) }))
  if (folded.length > 0) {
    const count = folded.reduce((n, f) => n + f.count, 0)
    // Several families have no single filter: the slice stays on this page, where the tree lists them all.
    slices.push({ key: '__other__', label: `Other (${folded.length} families)`, count, pct: share(count, total), other: true, drill: pageDrill(`${PATHS.os}${assetKind ? `?kind=${assetKind}` : ''}`, null) })
  }
  if (unknown) {
    slices.push({ key: 'unknown', label: 'Unknown', count: unknown.count, pct: share(unknown.count, total), unknown: true, drill: familyDrill(unknown) })
  }

  const legend: FamilyLegendRow[] = [...known, ...(unknown ? [unknown] : [])].map((f) => ({
    key: f.family,
    label: isUnknownFamily(f) ? 'Unknown' : f.label,
    count: f.count,
    pct: share(f.count, total),
    color: colour.get(f.family),
    unknown: isUnknownFamily(f) || undefined,
    tail: folded.includes(f) || undefined,
    drill: familyDrill(f),
  }))
  const noun = OS_KIND_OPTIONS.find((o) => o.value === kind)?.noun ?? 'assets'
  return { total, noun, slices, legend, folded: folded.length, other: slices.find((x) => x.other) ?? null }
}

/** The text alternative of the donut (its accessible name): "145 assets by operating system family: Windows 46 (31.7%), ...". */
export function donutSummary(m: FamilyDonutModel): string {
  return `${m.total} ${m.noun} by operating system family: ${m.legend.map((l) => `${l.label} ${l.count} (${fmtPct(l.pct)})`).join(', ')}`
}

/* ── Build currency ──────────────────────────────────────────────────────── */

export interface CurrencyRow {
  key: string
  label: string
  count: number
  /** The server could order the observed versions; null means it refused to (versions from different trains). */
  currency: OsCurrency | null
  /** Assets on the latest observed version: a list. */
  latest: { count: number; version: string; drill: Drill } | null
  /** Assets one version behind (N-1): a list. */
  previous: { count: number; version: string; drill: Drill } | null
  /** Assets further behind: text only (several versions: no single filter). */
  older: number
  /** Assets with no or an unparseable version: text only. */
  unknown: number
  /** Assets not on the latest observed version, unknown included: what ranks the row. */
  behind: number
  /** Shares of the release for the stacked bar, 0 to 100. */
  pct: { latest: number; previous: number; older: number; unknown: number }
}

/**
 * One row per release that has at least one identified version, the releases with most assets behind
 * the latest observed version first. A release whose versions the server could not order is kept, last,
 * with `currency: null`: saying so is more honest than leaving it out. "Latest observed" is the newest
 * version on the customer's own assets, NOT the vendor's latest release (lifecycle data replaces it in P4).
 */
export function currencyRows(os: OsResponse, kind: OsKindChoice = 'all'): CurrencyRow[] {
  const assetKind = osKindParam(kind)
  const out: CurrencyRow[] = []
  for (const f of os.families) {
    for (const p of f.products) {
      for (const r of p.releases) {
        if (r.distinct_versions === 0 && !r.currency) continue
        const c = r.currency
        const base = releaseFilters(assetKind, f, p, r)
        const part = (count: number, version: string | null): CurrencyRow['latest'] =>
          version === null ? null : { count, version, drill: assetsDrill({ ...base, os_version: version } as AssetFilters, count) }
        out.push({
          key: `${f.family}/${p.product ?? ''}/${r.release ?? ''}`,
          label: releaseLabel(f, p, r),
          count: r.count,
          currency: c,
          latest: c ? part(c.latest, c.latest_version) : null,
          previous: c && c.previous_version !== null ? part(c.previous, c.previous_version) : null,
          older: c?.older ?? 0,
          unknown: c?.unknown ?? 0,
          behind: c ? r.count - c.latest : 0,
          pct: c
            ? { latest: share(c.latest, r.count), previous: share(c.previous, r.count), older: share(c.older, r.count), unknown: share(c.unknown, r.count) }
            : { latest: 0, previous: 0, older: 0, unknown: 0 },
        })
      }
    }
  }
  return out.sort((a, b) => {
    if ((a.currency === null) !== (b.currency === null)) return a.currency === null ? 1 : -1
    return b.behind - a.behind || b.count - a.count || compareLabels(a.label, b.label)
  })
}

/** "42 on the latest observed build 10.0.20348.2700, 13 on N-1, 6 older": the same sentence for the page and a screen reader. */
export function currencySentence(r: CurrencyRow): string {
  if (!r.currency || !r.latest) return `${r.label}: ${r.count} assets; the observed versions cannot be ordered, so no build is called the latest`
  const parts = [`${r.latest.count} on the latest observed ${r.latest.version}`]
  if (r.previous) parts.push(`${r.previous.count} on N-1 (${r.previous.version})`)
  if (r.older > 0) parts.push(`${r.older} older`)
  if (r.unknown > 0) parts.push(`${r.unknown} with no usable version`)
  return `${r.label}: ${parts.join(', ')}`
}

/** The honest caveat shown under the build-currency card. */
export const CURRENCY_NOTE =
  'Latest observed is the newest version found on your own assets, not the vendor\'s latest release.'
