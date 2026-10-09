// Pure logic behind the Overview page (Documentation/Inventory/03-UI-UX-SPEC.md section 3.1): which
// number links where, how the OS trees become a donut and stacked bars, the health worklist and the
// sparkline rules. No React, no router, no API: scripts/test-inventory-ui.mjs exercises it directly,
// and the page only renders what it returns.
//
// Principle 1: every number is a link to the list behind it, and the drilled list's total EQUALS the
// number clicked. The table of 05-P1-API-CONTRACT.md section 4 is implemented here, one function per
// panel, so the mapping from a number to its filters lives in one place.

import { changesPageHref } from './changesModel'
import { ageWords, fmtInt, fmtShare, plural, share, type VersionEntry } from './model'
import {
  assetsHref, HEALTH_LABELS, HEALTH_PREDICATES, type AssetFilters, type HealthPredicate,
} from '@/pages/inventory/filters'
import type {
  HardwareResponse, InventorySummary, OsFamilyNode, OsProductNode, OsReleaseNode, OsResponse, SummaryProduct,
} from '@/pages/inventory/types'

/* ── Where a number goes ─────────────────────────────────────────────────── */

export const PATHS = {
  overview: '/inventory',
  assets: '/inventory/assets',
  os: '/inventory/os',
  software: '/inventory/software',
  hardware: '/inventory/hardware',
  changes: '/inventory/changes',
} as const

/** `/inventory/software/<product key>`: its version distribution has as many rows as the product has versions. */
export function productHref(productKey: string): string {
  return `${PATHS.software}/${encodeURIComponent(productKey)}`
}

/** What the Changes page filters by (the API's own names, see the contract). */
export interface ChangesFilters {
  category?: 'software' | 'os' | 'firmware' | 'hardware'
  action?: 'added' | 'removed' | 'changed'
  label?: 'upgrade' | 'downgrade'
  since?: string
}

/**
 * `/inventory/changes?...` for a number of the Overview. There is ONE builder of Changes addresses
 * (`changesPageHref` in ./changesModel.ts): this only narrows the filters an Overview number may carry.
 */
export function changesHref(f: ChangesFilters = {}): string {
  return changesPageHref(f)
}

/**
 * The target of one number. `assets` is a list of assets (the filters are the whole address);
 * `changes` and `page` open another Inventory page. `count` is the number the link stands for, or
 * null when the link is to a page that is not a list of that many things.
 */
export type Drill =
  | { type: 'assets'; filters: AssetFilters; count: number | null; href: string }
  | { type: 'changes'; filters: ChangesFilters; count: number | null; href: string }
  | { type: 'page'; count: number | null; href: string }

export const assetsDrill = (filters: AssetFilters, count: number | null): Drill => ({ type: 'assets', filters, count, href: assetsHref(filters) })
export const changesDrill = (filters: ChangesFilters, count: number | null): Drill => ({ type: 'changes', filters, count, href: changesHref(filters) })
export const pageDrill = (href: string, count: number | null = null): Drill => ({ type: 'page', count, href })

/* ── KPI row ─────────────────────────────────────────────────────────────── */

export interface KpiPart {
  /** The words around the number: "servers", "stale or never". */
  text: string
  /** The number, linked when `drill` is set. */
  value?: number
  drill?: Drill
}

export interface KpiModel {
  id: 'assets' | 'os' | 'software' | 'hardware' | 'fresh' | 'changes'
  label: string
  /** The big number as text (a percentage for the freshness card). */
  value: string
  /** The number behind `value`, or null when it is a derived figure. */
  valueCount: number | null
  drill: Drill
  /** Secondary figures, each a link where it is a list. */
  parts: KpiPart[]
  /** A line that says what the big number is a share of ("of 137 assets that can report"). */
  note?: string
  /** Sparkline metric, when trends can supply one. */
  trend?: 'assets' | 'software' | 'fresh' | 'changes'
}

export function kpiModels(s: InventorySummary): KpiModel[] {
  const c = s.coverage
  const byDesign = c.not_applicable
  const eligible = s.assets.total - byDesign
  const notReporting = c.stale + c.never
  const sw = s.changes_7d.software
  const softwareEvents = sw.added + sw.removed + sw.changed
  const since = s.changes_7d.since
  const changeParts: KpiPart[] = [
    { text: 'software', value: softwareEvents, drill: changesDrill({ category: 'software', since }, softwareEvents) },
    { text: 'firmware', value: s.changes_7d.firmware, drill: changesDrill({ category: 'firmware', since }, s.changes_7d.firmware) },
    { text: 'OS', value: s.changes_7d.os, drill: changesDrill({ category: 'os', since }, s.changes_7d.os) },
    { text: 'hardware', value: s.changes_7d.hardware, drill: changesDrill({ category: 'hardware', since }, s.changes_7d.hardware) },
  ].filter((p) => (p.value ?? 0) > 0)
  return [
    {
      id: 'assets',
      label: 'Assets',
      value: String(s.assets.total),
      valueCount: s.assets.total,
      drill: assetsDrill({}, s.assets.total),
      trend: 'assets',
      parts: [
        { text: 'servers', value: s.assets.server, drill: assetsDrill({ kind: 'server' }, s.assets.server) },
        { text: 'network', value: s.assets.device, drill: assetsDrill({ kind: 'device' }, s.assets.device) },
      ],
    },
    {
      id: 'os',
      label: 'OS releases',
      value: String(s.os.releases),
      valueCount: s.os.releases,
      drill: pageDrill(PATHS.os, s.os.releases),
      parts: [
        { text: 'families', value: s.os.families, drill: pageDrill(PATHS.os, s.os.families) },
        ...(s.os.unknown > 0 ? [{ text: 'unknown OS', value: s.os.unknown, drill: assetsDrill({ os_family: 'unknown' }, s.os.unknown) }] : []),
      ],
    },
    {
      id: 'software',
      label: 'Software titles',
      value: String(s.software.products),
      valueCount: s.software.products,
      drill: pageDrill(PATHS.software, s.software.products),
      trend: 'software',
      parts: [
        { text: 'installations', value: s.software.installs, drill: pageDrill(PATHS.software, s.software.installs) },
        { text: 'publishers', value: s.software.publishers, drill: pageDrill(PATHS.software, s.software.publishers) },
      ],
    },
    {
      id: 'hardware',
      label: 'Hardware models',
      value: String(s.hardware.models),
      valueCount: s.hardware.models,
      drill: pageDrill(PATHS.hardware, s.hardware.models),
      parts: [
        { text: 'vendors', value: s.hardware.vendors, drill: pageDrill(PATHS.hardware, s.hardware.vendors) },
        { text: 'network devices', value: s.hardware.devices, drill: assetsDrill({ kind: 'device' }, s.hardware.devices) },
      ],
    },
    {
      id: 'fresh',
      label: 'Fresh inventory',
      // The share of the assets that CAN report: those that cannot by design (not_applicable) are not in it.
      value: eligible > 0 ? fmtShare(c.fresh, eligible) : '—',
      valueCount: null,
      // A derived figure: it opens the list of fresh assets, whose total is the count below it.
      drill: assetsDrill({ freshness: 'fresh' }, c.fresh),
      trend: 'fresh',
      note: eligible > 0 ? `of ${plural(eligible, 'asset')} that can report` : 'no asset can report an inventory',
      parts: [
        { text: 'fresh', value: c.fresh, drill: assetsDrill({ freshness: 'fresh' }, c.fresh) },
        { text: 'stale or never', value: notReporting, drill: assetsDrill({ freshness: 'stale_or_never' }, notReporting) },
        ...(byDesign > 0 ? [{ text: 'not reported by design', value: byDesign, drill: assetsDrill({ freshness: 'not_applicable' }, byDesign) }] : []),
      ],
    },
    {
      id: 'changes',
      label: 'Changes, 7 days',
      value: String(s.changes_7d.total),
      valueCount: s.changes_7d.total,
      drill: changesDrill({ since }, s.changes_7d.total),
      trend: 'changes',
      parts: changeParts,
    },
  ]
}

/** Every drill the KPI row presents, flattened (the drill-down test walks this). */
export function kpiDrills(models: readonly KpiModel[]): Drill[] {
  return models.flatMap((m) => [m.drill, ...m.parts.flatMap((p) => (p.drill ? [p.drill] : []))])
}

/* ── Server OS: donut by family, legend by release ──────────────────────── */

/** Distinct slices before the rest fold into "other families" (CATEGORICAL has five colours). */
export const MAX_FAMILY_SLICES = 4
export const MAX_RELEASE_ROWS = 8

export interface OsSlice {
  key: string
  label: string
  count: number
  pct: number
  /** Index into CATEGORICAL for a known family; absent for the unknown and the folded slices. */
  color?: number
  unknown?: boolean
  other?: boolean
  drill: Drill
}

export interface OsReleaseRow {
  key: string
  label: string
  count: number
  pct: number
  /** The colour index of the family the release belongs to. */
  color?: number
  unknown?: boolean
  drill: Drill
}

export interface ServerOsModel {
  total: number
  slices: OsSlice[]
  releases: OsReleaseRow[]
  /** Releases (and their assets) that do not fit the legend; they are on the Operating Systems page. */
  more: { releases: number; assets: number; href: string } | null
}

const titleCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** "Windows Server 2022", "Ubuntu 22.04", "IOS XE (release unknown)": product and release as one name. */
export function releaseLabel(family: OsFamilyNode, product: OsProductNode, release: OsReleaseNode): string {
  const name = product.product ?? family.label ?? titleCase(family.family)
  return release.release ? `${name} ${release.release}` : `${name} (release unknown)`
}

/** The filters of a release node: the path to it, with `unknown` for a missing level. */
export function releaseFilters(kind: 'server' | 'device' | undefined, family: OsFamilyNode, product: OsProductNode, release: OsReleaseNode): AssetFilters {
  return {
    ...(kind ? { kind } : {}),
    os_family: family.family,
    os_product: product.product ?? 'unknown',
    os_release: release.release ?? 'unknown',
  }
}

/** Donut slices (families) and the legend (releases) for the servers of `GET /inventory/os?kind=server`. */
export function serverOsModel(os: OsResponse): ServerOsModel {
  const total = os.total
  const known = os.families.filter((f) => f.family !== 'unknown' && f.count > 0).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
  const unknownFamily = os.families.find((f) => f.family === 'unknown')
  const keep = known.length > MAX_FAMILY_SLICES + 1 ? known.slice(0, MAX_FAMILY_SLICES) : known
  const folded = known.slice(keep.length)
  const colour = new Map(keep.map((f, i) => [f.family, i]))

  const slices: OsSlice[] = keep.map((f, i) => ({
    key: f.family, label: f.label, count: f.count, pct: share(f.count, total), color: i,
    drill: assetsDrill({ kind: 'server', os_family: f.family }, f.count),
  }))
  if (folded.length > 0) {
    const count = folded.reduce((n, f) => n + f.count, 0)
    slices.push({
      key: '__other__', label: `Other (${folded.length} families)`, count, pct: share(count, total), other: true,
      // Several families have no single filter: the page that lists them.
      drill: pageDrill(`${PATHS.os}?kind=server`),
    })
  }
  if (unknownFamily && unknownFamily.count > 0) {
    slices.push({
      key: 'unknown', label: 'Unknown', count: unknownFamily.count, pct: share(unknownFamily.count, total), unknown: true,
      drill: assetsDrill({ kind: 'server', os_family: 'unknown' }, unknownFamily.count),
    })
  }

  const all: OsReleaseRow[] = []
  for (const f of known) {
    for (const p of f.products) {
      for (const r of p.releases) {
        all.push({
          key: `${f.family}/${p.product ?? ''}/${r.release ?? ''}`,
          label: releaseLabel(f, p, r),
          count: r.count,
          pct: share(r.count, total),
          color: colour.get(f.family),
          drill: assetsDrill(releaseFilters('server', f, p, r), r.count),
        })
      }
    }
  }
  all.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, undefined, { numeric: true }))
  const releases = all.slice(0, MAX_RELEASE_ROWS)
  const rest = all.slice(MAX_RELEASE_ROWS)
  if (unknownFamily && unknownFamily.count > 0) {
    releases.push({
      key: 'unknown', label: 'Unknown', count: unknownFamily.count, pct: share(unknownFamily.count, total), unknown: true,
      drill: assetsDrill({ kind: 'server', os_family: 'unknown' }, unknownFamily.count),
    })
  }
  return {
    total,
    slices,
    releases,
    more: rest.length > 0 ? { releases: rest.length, assets: rest.reduce((n, r) => n + r.count, 0), href: `${PATHS.os}?kind=server` } : null,
  }
}

/* ── Network OS and firmware: one stacked bar per OS product ───────────── */

export const MAX_FIRMWARE_PRODUCTS = 5

export interface FirmwareRow {
  key: string
  /** "FortiOS", "IOS XE"; the family label when a family has no product. */
  label: string
  family: string
  count: number
  /** Distinct known versions across the product's releases. */
  versions: number
  /** The product's assets, whatever the version: the "other" bucket and the name link here. */
  drill: Drill
  /** Versions newest first (the API's order), as the version bar wants them. */
  entries: VersionEntry[]
  unknown?: boolean
}

export interface NetworkOsModel {
  total: number
  rows: FirmwareRow[]
  more: { products: number; assets: number; href: string } | null
}

/** Newest first: the API orders releases and versions that way; a rank keeps that order in the bar. */
export function firmwareEntries(family: OsFamilyNode, product: OsProductNode): VersionEntry[] {
  const out: VersionEntry[] = []
  const base: AssetFilters = { kind: 'device', os_family: family.family, os_product: product.product ?? 'unknown' }
  const unknownHref = assetsHref({ ...base, os_version: 'unknown' })
  let rank = product.releases.reduce((n, r) => n + r.versions.length, 0)
  for (const r of product.releases) {
    for (const v of r.versions) {
      const known = v.version !== null
      out.push({
        key: `${r.release ?? ''}|${v.version ?? ''}`,
        label: v.version ?? 'Version unknown',
        count: v.count,
        rank: rank--,
        unknown: !known,
        // Unknown versions of every release merge into one segment, so they link to the one list that holds them all.
        href: known ? assetsHref({ ...base, os_release: r.release ?? 'unknown', os_version: v.version as string }) : unknownHref,
      })
    }
  }
  return out
}

/** Network devices (`GET /inventory/os?kind=device`): the products, biggest first, the unknown OS last and still a row. */
export function networkOsModel(os: OsResponse): NetworkOsModel {
  const rows: FirmwareRow[] = []
  let unknown: FirmwareRow | null = null
  for (const f of os.families) {
    if (f.family === 'unknown') {
      if (f.count > 0) {
        unknown = {
          key: 'unknown', label: 'Unknown OS', family: 'unknown', count: f.count, versions: 0, unknown: true, entries: [],
          drill: assetsDrill({ kind: 'device', os_family: 'unknown' }, f.count),
        }
      }
      continue
    }
    for (const p of f.products) {
      if (p.count <= 0) continue
      const entries = firmwareEntries(f, p)
      rows.push({
        key: `${f.family}/${p.product ?? ''}`,
        label: p.product ?? f.label,
        family: f.family,
        count: p.count,
        versions: new Set(p.releases.flatMap((r) => r.versions.filter((v) => v.version !== null).map((v) => `${r.release}|${v.version}`))).size,
        entries,
        drill: assetsDrill({ kind: 'device', os_family: f.family, os_product: p.product ?? 'unknown' }, p.count),
      })
    }
  }
  rows.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
  const shown = rows.slice(0, MAX_FIRMWARE_PRODUCTS)
  const rest = rows.slice(MAX_FIRMWARE_PRODUCTS)
  if (unknown) shown.push(unknown)
  return {
    total: os.total,
    rows: shown,
    more: rest.length > 0 ? { products: rest.length, assets: rest.reduce((n, r) => n + r.count, 0), href: `${PATHS.os}?kind=device` } : null,
  }
}

/* ── Software ────────────────────────────────────────────────────────────── */

export interface SoftwareRow {
  key: string
  name: string
  category: string | null
  hosts: number
  versions: number
  /** Share of the longest bar in the panel, 0 to 100 (hosts in "top", versions in "sprawl"). */
  barPct: number
  hostsDrill: Drill
  /** The product page: its version distribution has `versions` rows. */
  versionsDrill: Drill
}

export function softwareRows(products: readonly SummaryProduct[], by: 'hosts' | 'versions'): SoftwareRow[] {
  const max = Math.max(1, ...products.map((p) => p[by]))
  return products.map((p) => ({
    key: p.product_key,
    name: p.name,
    category: p.category,
    hosts: p.hosts,
    versions: p.versions,
    barPct: share(p[by], max),
    hostsDrill: assetsDrill({ product: p.product_key }, p.hosts),
    versionsDrill: pageDrill(productHref(p.product_key), p.versions),
  }))
}

export interface AgentCoverage {
  /** Servers with endpoint protection. */
  covered: number
  /** Servers with a complete software list and no endpoint protection. */
  missing: number
  /** Servers whose software list is incomplete (truncated, not reported yet or not yet normalised): nothing can be said. */
  unknown: number
  /** covered + missing: the servers the question can be answered for. The "of N" of "on 39 of 52". */
  answerable: number
  /** Every server. */
  total: number
  /** covered of answerable, 0 to 100. */
  pct: number
  coveredDrill: Drill
  gapDrill: Drill
  unknownDrill: Drill
}

/**
 * "Endpoint protection on 39 of 52 servers with a complete software list". Endpoint protection is a product
 * of the category `endpoint-protection` (antivirus, EPP and EDR engines), or a running Defender service.
 * "52" is covered + missing, NOT every server: a server whose software list is truncated or not yet normalised
 * is UNKNOWN for this question (its own line, drillable), never counted as missing. The three figures each
 * open their own list and sum to every server.
 */
export function agentCoverage(s: InventorySummary): AgentCoverage | null {
  const { covered, missing, unknown, total } = s.software.security_agent_coverage
  if (total <= 0) return null
  const answerable = covered + missing
  return {
    covered, missing, unknown, answerable, total, pct: share(covered, answerable),
    coveredDrill: assetsDrill({ security_agent: 'present' }, covered),
    gapDrill: assetsDrill({ security_agent: 'missing' }, missing),
    unknownDrill: assetsDrill({ security_agent: 'unknown' }, unknown),
  }
}

/**
 * Words for the unknown servers, one place: Overview line, flyout and docs agree. A server is unknown for
 * endpoint protection when no complete software list (or, for built-in Defender, no service list) is there to judge.
 * That is not always a state that passes: a server whose agent does not report software at all (Linux today) stays
 * unknown, so the sentence names the missing evidence instead of promising it will arrive.
 */
export function unknownServersText(n: number): string {
  return `${plural(n, 'server')} unknown: no complete software or service list to judge`
}

/** The tooltip of the unknown line: every reason a server can be unknown, none of them promised to pass. */
export const UNKNOWN_SERVERS_HINT =
  'No software list has been reported (agents that do not report software, such as Linux servers today, stay here), the list was cut off at the agent cap, the list is still being processed, or built-in Defender cannot be judged because no service list was reported. Open the list; each server names its reason.'

/**
 * The line under the version-sprawl table. Sprawl ranks products by how many DIFFERENT versions are installed,
 * which is a sign of upgrade drift, not a count of hosts behind: the product page has that. When the server
 * could only rank the most installed products (a very large catalog) the line says so (review findings UI-06, BN-02).
 */
export function sprawlNote(basis: InventorySummary['software']['sprawl_basis'] | undefined): string {
  const base = 'Ranked by how many different versions are installed, a sign of upgrade drift. Open a product to see how many hosts are behind the newest version.'
  if (basis && basis.scope === 'top_installed') {
    return `${base} Only the ${fmtInt(basis.candidates)} most installed of ${fmtInt(basis.products)} products were ranked.`
  }
  return base
}

/* ── Hardware ────────────────────────────────────────────────────────────── */

export interface ModelRow {
  key: string
  vendor: string
  model: string
  count: number
  barPct: number
  drill: Drill
}

export function topModels(s: InventorySummary): ModelRow[] {
  const rows = s.hardware.top_models
  const max = Math.max(1, ...rows.map((m) => m.count))
  return rows.map((m) => ({
    key: `${m.vendor}/${m.model}`,
    vendor: m.vendor,
    model: m.model,
    count: m.count,
    barPct: share(m.count, max),
    drill: assetsDrill({ kind: 'device', vendor: m.vendor, model: m.model }, m.count),
  }))
}

export interface MemoryRow {
  value: string
  label: string
  count: number
  pct: number
  unknown: boolean
  drill: Drill
}

/** Servers by installed memory (the six buckets, then Unknown when any): `GET /inventory/hardware` `servers.memory`. */
export function memoryRows(h: HardwareResponse): MemoryRow[] {
  const total = h.servers.total
  return h.servers.memory
    .filter((b) => b.value !== 'unknown' || b.count > 0)
    .map((b) => ({
      value: b.value, label: b.label, count: b.count, pct: share(b.count, total), unknown: b.value === 'unknown',
      drill: assetsDrill({ memory: b.value as AssetFilters['memory'] }, b.count),
    }))
}

/* ── Inventory health ────────────────────────────────────────────────────── */

/** The nine lines of the health panel: the eight predicates, then the assets that are not collected by design. */
export type HealthItemKey = HealthPredicate | 'not_collected_by_design'

export interface HealthItem {
  key: HealthItemKey
  count: number
  /** "network devices missing a model": the words after the number. */
  text: string
  /** The sentence for a predicate that matches nothing ("No network device is missing a model"). */
  none: string
  /** Which kinds the predicate looks at (scope decision S3). */
  scope: 'device' | 'server' | 'both'
  /** What to do about it: the cause differs by line, so the advice does too. */
  hint?: string
  /** An informational line (not a gap): it never counts against the all-clear. */
  info?: boolean
  drill: Drill
}

const HEALTH_TEXT: Record<HealthPredicate, { scope: HealthItem['scope']; one: string; many: string; none: string; hint?: string }> = {
  missing_os_version: {
    scope: 'both', one: 'asset missing an OS version', many: 'assets missing an OS version', none: 'No asset is missing an OS version',
    hint: 'Assets that cannot report by design are not counted.',
  },
  missing_model: {
    scope: 'device', one: 'network device missing a model', many: 'network devices missing a model', none: 'No network device is missing a model',
    hint: 'Make sure the SNMP view includes ENTITY-MIB. Ping-only and controller-managed devices are not counted.',
  },
  missing_serial: {
    scope: 'device', one: 'network device missing a serial number', many: 'network devices missing a serial number', none: 'No network device is missing a serial number',
    hint: 'Make sure the SNMP view includes ENTITY-MIB. Ping-only and controller-managed devices are not counted.',
  },
  unknown_vendor: {
    scope: 'device', one: 'network device with an unknown vendor', many: 'network devices with an unknown vendor', none: 'Every network device has a known vendor',
    hint: 'Ping-only and controller-managed devices are not counted.',
  },
  stale: { scope: 'both', one: 'asset with stale inventory', many: 'assets with stale inventory', none: 'No asset has stale inventory' },
  never: {
    scope: 'both', one: 'asset that can report but never has', many: 'assets that can report but never have', none: 'Every asset that can report has reported at least once',
    hint: 'Servers: install or repair the agent. Network devices: check that SNMP answers. Assets that cannot report by design are listed separately.',
  },
  software_truncated: { scope: 'server', one: 'server with a truncated software list', many: 'servers with a truncated software list', none: 'No server has a truncated software list' },
  missing_security_agent: {
    scope: 'server', one: 'server without endpoint protection', many: 'servers without endpoint protection', none: 'Every server with a complete software list has endpoint protection',
    hint: 'Only servers with a complete software list (and, for built-in Defender, a service list) are counted; the others are unknown, not missing.',
  },
}

const BY_DESIGN_TEXT = {
  one: 'asset not reported by design',
  many: 'assets not reported by design',
  none: 'Every asset can report an inventory',
  hint: 'Ping-only and controller-managed devices, and servers whose collection mode cannot upload an inventory. Listed here, not counted as never reported.',
} as const

/** The eight predicates in one fixed order, each with its count (a predicate the API did not send counts as absent), then the by-design line. */
export function healthItems(s: InventorySummary): HealthItem[] {
  const items = HEALTH_PREDICATES.flatMap((key): HealthItem[] => {
    const count = s.health[key]
    if (typeof count !== 'number') return []
    const t = HEALTH_TEXT[key]
    return [{ key, count, text: count === 1 ? t.one : t.many, none: t.none, scope: t.scope, hint: t.hint, drill: assetsDrill({ health: key }, count) }]
  })
  const byDesign = s.health.not_collected_by_design?.count
  if (typeof byDesign === 'number') {
    items.push({
      key: 'not_collected_by_design', count: byDesign, text: byDesign === 1 ? BY_DESIGN_TEXT.one : BY_DESIGN_TEXT.many, none: BY_DESIGN_TEXT.none,
      scope: 'both', hint: BY_DESIGN_TEXT.hint, info: true, drill: assetsDrill({ freshness: 'not_applicable' }, byDesign),
    })
  }
  return items
}

/**
 * The all-clear state: assets exist, not one of the eight predicates matches and the figures are complete.
 * Assets that are not collected by design do not prevent it (they are not a gap), and while the existing
 * software lists are still being normalised nothing can be called complete.
 */
export function healthAllClear(s: InventorySummary): boolean {
  if (s.assets.total === 0 || s.data_status?.normalising) return false
  return healthItems(s).every((i) => i.info || i.count === 0)
}

export function healthLabel(key: HealthPredicate): string {
  return HEALTH_LABELS[key]
}

/* ── Sparklines (scope decision S4) ──────────────────────────────────────── */

export interface DayValue {
  day: string
  value: number
}

/** A KPI sparkline needs at least this many days of snapshots; with fewer there is no line and no placeholder. */
export const MIN_TREND_DAYS = 3

interface RawSeries {
  metric: string
  dimension: string
  points: DayValue[]
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

function readPoints(v: unknown): DayValue[] {
  if (!Array.isArray(v)) return []
  const out: DayValue[] = []
  for (const p of v) {
    if (Array.isArray(p) && typeof p[0] === 'string' && typeof p[1] === 'number') out.push({ day: p[0].slice(0, 10), value: p[1] })
    else if (isObject(p)) {
      const day = p.day ?? p.date
      const value = p.value ?? p.v
      if (typeof day === 'string' && typeof value === 'number' && Number.isFinite(value)) out.push({ day: day.slice(0, 10), value })
    }
  }
  return out
}

/**
 * Reads a `GET /inventory/trends` answer without trusting its shape. Returns null when the caller is
 * restricted (fleet-wide history is never given to a scoped user) or the answer holds no series.
 */
export function readTrends(raw: unknown): RawSeries[] | null {
  // `enabled: false` is the owner's display switch (recording continues): no line.
  if (!isObject(raw) || raw.restricted === true || raw.enabled === false) return null
  const list = Array.isArray(raw.series) ? raw.series : Array.isArray(raw.points) ? [raw] : []
  const out: RawSeries[] = []
  for (const s of list) {
    if (!isObject(s)) continue
    out.push({ metric: String(s.metric ?? ''), dimension: String(s.dimension ?? ''), points: readPoints(s.points ?? s.data ?? s.values) })
  }
  return out.length > 0 ? out : null
}

function byDay(series: readonly RawSeries[], pick: (s: RawSeries) => boolean): Map<string, number> {
  const days = new Map<string, number>()
  for (const s of series) {
    if (!pick(s)) continue
    for (const p of s.points) days.set(p.day, (days.get(p.day) ?? 0) + p.value)
  }
  return days
}

const asSeries = (days: Map<string, number>): DayValue[] => [...days.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([day, value]) => ({ day, value }))

/** The sum of every dimension of a metric, per day (assets: servers plus devices; changes: every category and action). */
export function sumSeries(series: readonly RawSeries[] | null, dimensions?: readonly string[]): DayValue[] {
  if (!series) return []
  return asSeries(byDay(series, (s) => !dimensions || dimensions.includes(s.dimension)))
}

/** The last day of the window the answer covers (today, appliance-local), or null when it does not say. */
export function trendUntil(raw: unknown): string | null {
  const w = isObject(raw) && isObject(raw.window) ? raw.window.until : undefined
  return typeof w === 'string' ? w.slice(0, 10) : null
}

/**
 * Leaves out one day. The change counts of today are written a quarter of an hour into the day and only
 * complete once it is over, so a per-day change line would end in a false dip.
 */
export function withoutDay(points: readonly DayValue[], day: string | null): DayValue[] {
  return day ? points.filter((p) => p.day !== day) : [...points]
}

/** The share of fresh assets per day, from the coverage family (fresh, aging, stale, never). */
export function freshShareSeries(series: readonly RawSeries[] | null): DayValue[] {
  if (!series) return []
  const fresh = byDay(series, (s) => s.dimension === 'fresh')
  const all = byDay(series, (s) => ['fresh', 'aging', 'stale', 'never'].includes(s.dimension))
  return asSeries(all).flatMap(({ day, value }) => (value > 0 ? [{ day, value: ((fresh.get(day) ?? 0) / value) * 100 }] : []))
}

/** Whether a line may be drawn at all: at least {@link MIN_TREND_DAYS} distinct days. */
export function canDrawTrend(points: readonly DayValue[]): boolean {
  return new Set(points.map((p) => p.day)).size >= MIN_TREND_DAYS
}

/** Screen-reader text for a sparkline: "Assets over 30 days: from 141 to 145". */
export function trendSummary(label: string, points: readonly DayValue[], fmt: (n: number) => string = String): string {
  if (points.length === 0) return label
  const first = points[0]
  const last = points[points.length - 1]
  return `${label} over ${points.length} days: from ${fmt(first.value)} to ${fmt(last.value)}`
}

/* ── Wording ─────────────────────────────────────────────────────────────── */

/** "as of 5m ago" for the stale-data banner. */
export function dataAge(generatedAt: string | null | undefined, now: number): string {
  return generatedAt ? ageWords(generatedAt, now) : 'unknown'
}
