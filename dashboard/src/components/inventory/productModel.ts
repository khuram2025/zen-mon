// Pure logic behind the product page (Documentation/Inventory/03-UI-UX-SPEC.md section 3.5,
// 05-P1-API-CONTRACT.md sections 3.10, 3.11 and 4): tabs and their URL state, how versions are ranked and
// labelled, the header figures, the installation list, the timeline and the evidence. No React, no router, no
// API: scripts/test-inventory-software-settings.mjs exercises it directly.
//
// Principle 1 holds here too: a host count of a version is a link to the assets with that version, its
// installation count a link to the Installed on list with that version. The numbers with no list of their own
// (unversioned hosts, the evidence counts, "behind latest") stay text.

import { assetsHref, exactText, freeText, parseListState, type SortOrder } from '@/pages/inventory/filters'
import { NO_VERSION, type EvidenceItem, type InstallationRow, type ProductResponse, type ProductVersion, type VersionScheme } from '@/pages/inventory/softwareSettings'
import { changesPageHref } from './changesModel'
import { fmtInt, plural } from './model'
import { firstSeenText, productPath } from './softwareModel'

/* ── Tabs and URL state ──────────────────────────────────────────────────── */

export const PRODUCT_TABS = ['versions', 'installed', 'timeline', 'evidence'] as const
export type ProductTab = (typeof PRODUCT_TABS)[number]
export const DEFAULT_PRODUCT_TAB: ProductTab = 'versions'

export const TAB_LABELS: Record<ProductTab, string> = {
  versions: 'Versions',
  installed: 'Installed on',
  timeline: 'Timeline',
  evidence: 'Evidence',
}

export function parseProductTab(raw: string | null | undefined): ProductTab {
  const t = (raw ?? '').trim().toLowerCase()
  return (PRODUCT_TABS as readonly string[]).includes(t) ? (t as ProductTab) : DEFAULT_PRODUCT_TAB
}

export const INSTALL_SORT_KEYS = ['asset_name', 'package_name', 'version', 'architecture', 'source', 'install_date', 'first_seen_at', 'updated_at'] as const
export type InstallSort = (typeof INSTALL_SORT_KEYS)[number]
export const INSTALL_DEFAULT_SORT: { sort: InstallSort; order: SortOrder } = { sort: 'asset_name', order: 'asc' }
export const INSTALL_DEFAULT_PAGE_SIZE = 50
/** The installation list sorts by version only within this many rows per product (422 past it). */
export const VERSION_SORT_LIMIT = 50_000

export const TIMELINE_DAYS = [7, 30, 90] as const
export type TimelineDays = (typeof TIMELINE_DAYS)[number]
export const DEFAULT_TIMELINE_DAYS: TimelineDays = 7

/** What the Installed on tab filters by: the API's own names (`version`, `q`, `sort`, `order`, `page`, `page_size`). */
export interface InstalledState {
  /** A version (every spelling of it), or `NO_VERSION` (`__none__`) for the rows with none. */
  version?: string
  q?: string
  sort: InstallSort
  order: SortOrder
  page: number
  pageSize: number
}

export interface ProductState {
  tab: ProductTab
  installed: InstalledState
  /** Timeline window in days, page and page size. */
  days: TimelineDays
  page: number
  pageSize: number
}

/**
 * A version is an EXACT value (only its ends are trimmed: `2.0 beta  2` really has two spaces, FE-04), the search is
 * words. Over the API's 200 characters, or with a control character, the value is dropped (the page shows the
 * default list instead of a 422).
 */
const cleanExact = (raw: string | null): string | undefined => {
  const t = exactText(raw ?? '', 200)
  return 'value' in t ? t.value : undefined
}
const cleanWords = (raw: string | null): string | undefined => {
  const t = freeText(raw ?? '', 200)
  return 'value' in t ? t.value : undefined
}

export function parseTimelineDays(raw: string | null | undefined): TimelineDays {
  const n = Number(raw)
  return (TIMELINE_DAYS as readonly number[]).includes(n) ? (n as TimelineDays) : DEFAULT_TIMELINE_DAYS
}

export function readProductState(params: URLSearchParams): ProductState {
  const list = parseListState(params, INSTALL_SORT_KEYS) as { sort?: InstallSort; order?: SortOrder; page?: number; page_size?: number }
  return {
    tab: parseProductTab(params.get('tab')),
    installed: {
      version: cleanExact(params.get('version')),
      q: cleanWords(params.get('q')),
      sort: list.sort ?? INSTALL_DEFAULT_SORT.sort,
      order: list.order ?? INSTALL_DEFAULT_SORT.order,
      page: list.page ?? 1,
      pageSize: list.page_size ?? INSTALL_DEFAULT_PAGE_SIZE,
    },
    days: parseTimelineDays(params.get('days')),
    page: list.page ?? 1,
    pageSize: list.page_size ?? INSTALL_DEFAULT_PAGE_SIZE,
  }
}

/** Parameters that belong to the Installed on list: they are dropped when another tab opens. */
const INSTALLED_KEYS = ['version', 'q', 'sort', 'order', 'page_size']
const TAB_PARAMS = ['tab', 'days', 'page', ...INSTALLED_KEYS]

/** A tab is a different table: it starts clean (own sort, page and filters). */
export function withProductTab(params: URLSearchParams, tab: ProductTab): URLSearchParams {
  const next = new URLSearchParams(params)
  for (const k of TAB_PARAMS) next.delete(k)
  if (tab !== DEFAULT_PRODUCT_TAB) next.set('tab', tab)
  return next
}

/** The Installed on list, with its filters, as the parameters of an address (defaults are not written). */
export function writeInstalledState(state: Partial<InstalledState>, into: URLSearchParams = new URLSearchParams()): URLSearchParams {
  for (const k of [...INSTALLED_KEYS, 'page']) into.delete(k)
  into.set('tab', 'installed')
  if (state.version) into.set('version', state.version)
  if (state.q) into.set('q', state.q)
  const sort = state.sort ?? INSTALL_DEFAULT_SORT.sort
  const order = state.order ?? INSTALL_DEFAULT_SORT.order
  if (sort !== INSTALL_DEFAULT_SORT.sort || order !== INSTALL_DEFAULT_SORT.order) {
    into.set('sort', sort)
    into.set('order', order)
  }
  if ((state.page ?? 1) > 1) into.set('page', String(state.page))
  if ((state.pageSize ?? INSTALL_DEFAULT_PAGE_SIZE) !== INSTALL_DEFAULT_PAGE_SIZE) into.set('page_size', String(state.pageSize))
  return into
}

/** Changes to the Installed on list that keep the other parameters of the address. */
export function withInstalled(params: URLSearchParams, patch: Partial<InstalledState>, options: { keepPage?: boolean } = {}): URLSearchParams {
  const current = readProductState(params).installed
  // `undefined` in a patch removes that filter (the spread copies it over the current value).
  const merged: InstalledState = { ...current, ...patch, page: options.keepPage ? (patch.page ?? current.page) : 1 }
  const next = new URLSearchParams(params)
  writeInstalledState(merged, next)
  return next
}

export function nextInstallSort(current: { sort: string; order: SortOrder }, column: InstallSort): { sort: InstallSort; order: SortOrder } {
  if (current.sort === column) return { sort: column, order: current.order === 'asc' ? 'desc' : 'asc' }
  // Dates start with the newest, everything else A to Z.
  return { sort: column, order: column === 'install_date' || column === 'first_seen_at' || column === 'updated_at' ? 'desc' : 'asc' }
}

export function withInstallSort(params: URLSearchParams, column: InstallSort): URLSearchParams {
  const s = readProductState(params).installed
  return withInstalled(params, nextInstallSort(s, column))
}

export function withTimelineDays(params: URLSearchParams, days: TimelineDays): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete('page')
  if (days === DEFAULT_TIMELINE_DAYS) next.delete('days')
  else next.set('days', String(days))
  return next
}

export function withTimelinePage(params: URLSearchParams, page: number): URLSearchParams {
  const next = new URLSearchParams(params)
  if (page > 1) next.set('page', String(Math.floor(page)))
  else next.delete('page')
  return next
}

export function withTimelinePageSize(params: URLSearchParams, pageSize: number): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete('page')
  const size = Math.max(1, Math.min(500, Math.floor(pageSize) || INSTALL_DEFAULT_PAGE_SIZE))
  if (size === INSTALL_DEFAULT_PAGE_SIZE) next.delete('page_size')
  else next.set('page_size', String(size))
  return next
}

/** The Installed on tab of a product, optionally on one version: the list a version's installs number opens. */
export function installedHref(productKey: string, version?: string): string {
  const p = writeInstalledState({ version })
  return `${productPath(productKey)}?${p.toString()}`
}

/** The Versions tab (the default tab has no `tab` parameter). */
export function versionsHref(productKey: string): string {
  return productPath(productKey)
}

export function tabHref(productKey: string, tab: ProductTab): string {
  return productPath(productKey, tab === DEFAULT_PRODUCT_TAB ? {} : { tab })
}

/* ── Versions ────────────────────────────────────────────────────────────── */

export const SCHEME_LABELS: Record<VersionScheme, { title: string; note: string }> = {
  semverish: { title: 'Windows and other installers', note: 'Ranked by version number' },
  dpkg: { title: 'Debian and Ubuntu packages', note: 'Ranked by package version (dpkg)' },
  rpm: { title: 'RPM packages', note: 'Ranked by package version (rpm)' },
}

export interface GenerationMeta {
  label: string
  tone: string
  title: string
}

/**
 * The pill of a version: N (the newest observed), N-1, Older. These are observed generations, not vendor
 * releases (the lifecycle-aware currency comes with a later phase). A version the scheme cannot order has no
 * generation: it says so instead of guessing.
 */
export function generationMeta(v: Pick<ProductVersion, 'generation' | 'rank'>): GenerationMeta {
  switch (v.generation) {
    case 'N':
      return { label: 'N', tone: 'border-success/30 bg-success/10 text-success-fg', title: 'The newest version seen on a visible server' }
    case 'N-1':
      return { label: 'N-1', tone: 'border-info/30 bg-info/10 text-info-fg', title: 'The second newest version seen' }
    case 'older':
      return { label: 'Older', tone: 'border-warning/35 bg-warning/10 text-warning-fg', title: 'Two or more versions behind the newest seen' }
    default:
      return { label: 'Unordered', tone: 'border-border bg-surface2 text-text2', title: 'This version cannot be ordered against the others, so it has no generation' }
  }
}

export interface VersionGroup {
  scheme: VersionScheme
  versions: ProductVersion[]
  hosts: number
}

/** Consecutive versions of one scheme form a group, in the server's order (the group with more installs first). */
export function versionGroups(versions: readonly ProductVersion[]): VersionGroup[] {
  const groups: VersionGroup[] = []
  for (const v of versions) {
    const last = groups[groups.length - 1]
    if (last && last.scheme === v.scheme) {
      last.versions.push(v)
      last.hosts += v.hosts
    } else {
      groups.push({ scheme: v.scheme, versions: [v], hosts: v.hosts })
    }
  }
  return groups
}

/** The newest version row of the product (the main group's rank 1 spelled as `header.latest_version`). */
export function latestRow(header: Pick<ProductResponse['header'], 'latest_version'>, versions: readonly ProductVersion[]): ProductVersion | null {
  if (!header.latest_version) return null
  return versions.find((v) => v.version === header.latest_version) ?? null
}

/**
 * Hosts with no copy of the newest version: `hosts` minus the hosts that run it. It is not a list total (the filter
 * vocabulary cannot say "not this version"), so the page shows it as text. It is only given for a product whose
 * versions share one scheme: a Windows host and an Ubuntu host are not behind the same release.
 */
export function behindLatest(header: Pick<ProductResponse['header'], 'hosts' | 'latest_version' | 'mixed_schemes'>, versions: readonly ProductVersion[]): number | null {
  if (header.mixed_schemes) return null
  const latest = latestRow(header, versions)
  if (!latest) return null
  return Math.max(0, header.hosts - latest.hosts)
}

export interface ProductKpi {
  id: 'hosts' | 'versions' | 'latest' | 'behind'
  label: string
  /** The big figure as text. */
  value: string
  /** The line under it. */
  sub: string
  /** Where the figure leads, when a list stands behind it. */
  href: string | null
  /** The number the link stands for (written to data-drill-count). */
  count: number | null
  /** How the figure is set: version strings are monospaced. */
  mono?: boolean
  /** Why the figure has no link, for the tooltip. */
  note?: string
}

export function productKpis(data: Pick<ProductResponse, 'header' | 'versions' | 'unversioned'>): ProductKpi[] {
  const { header, versions } = data
  const key = header.product_key
  const latest = latestRow(header, versions)
  const behind = behindLatest(header, versions)
  const kpis: ProductKpi[] = [
    {
      id: 'hosts',
      label: 'Hosts',
      value: fmtInt(header.hosts),
      sub: header.installs === header.hosts ? 'servers with it installed' : `${plural(header.installs, 'installation')} on ${plural(header.hosts, 'server')}`,
      href: assetsHref({ product: key }),
      count: header.hosts,
    },
    {
      id: 'versions',
      label: 'Versions',
      value: fmtInt(header.version_count),
      sub: data.unversioned.installs > 0 ? `plus ${plural(data.unversioned.installs, 'installation')} with none reported` : 'distinct versions',
      href: versionsHref(key),
      count: header.version_count,
    },
    {
      id: 'latest',
      label: 'Latest seen',
      value: header.latest_version ?? '—',
      sub: header.latest_version
        ? latest ? `${plural(latest.hosts, 'host')} on it` : 'newest version seen'
        : 'no version can be ordered',
      href: header.latest_version ? assetsHref({ product: key, product_version: header.latest_version }) : null,
      count: latest ? latest.hosts : null,
      mono: true,
    },
    {
      id: 'behind',
      label: 'Behind latest',
      value: behind === null ? '—' : fmtInt(behind),
      sub: behind === null
        ? header.mixed_schemes ? 'versions of different schemes are ranked separately' : 'no newest version to compare with'
        : 'hosts without the newest version',
      href: null,
      count: null,
      note: 'There is no list of this number: a host that runs two versions counts once, and the filters cannot say "not this version".',
    },
  ]
  return kpis
}

/**
 * The two facts under the product name; `lastChange` is null when no change was ever recorded. A first-seen at or
 * before `historyStart` (when change capture began) reads "First seen before <date>": it was already there.
 */
export function productFacts(header: Pick<ProductResponse['header'], 'first_seen' | 'last_change'>, shortDate: (iso: string | null) => string, historyStart?: string | null): { firstSeen: string; lastChange: string | null } {
  return {
    firstSeen: historyStart ? firstSeenText(header.first_seen, historyStart) : header.first_seen ? `First seen ${shortDate(header.first_seen)}` : 'First seen unknown',
    lastChange: header.last_change ? `last change ${shortDate(header.last_change)}` : null,
  }
}

/** The same facts as one line. */
export function productFactLine(header: Pick<ProductResponse['header'], 'first_seen' | 'last_change'>, shortDate: (iso: string | null) => string, historyStart?: string | null): string {
  const f = productFacts(header, shortDate, historyStart)
  return `${f.firstSeen} · ${f.lastChange ?? 'no change recorded yet'}`
}

/* ── Chart rows ──────────────────────────────────────────────────────────── */

export interface VersionBarRow {
  key: string
  version: string
  hosts: number
  installs: number
  scheme: VersionScheme
  generation: GenerationMeta
  /** The server's generation word (N, N-1, older, or null when the scheme cannot order it): drives the tint. */
  generationKey: ProductVersion['generation']
  /** Width of the bar relative to the busiest version, 0 to 100. */
  width: number
  hostsHref: string
  installsHref: string
  /** Accessible names of the two number links of the table twin: the digits alone say nothing out of their row (UI-15). */
  hostsLabel: string
  installsLabel: string
}

/** Rows of the chart and of its table twin: one per version, in the server's order. */
export function versionRows(productKey: string, versions: readonly ProductVersion[]): VersionBarRow[] {
  const max = Math.max(1, ...versions.map((v) => v.hosts))
  return versions.map((v, i) => ({
    key: `${v.scheme}:${v.version}:${i}`,
    version: v.version,
    hosts: v.hosts,
    installs: v.installs,
    scheme: v.scheme,
    generation: generationMeta(v),
    generationKey: v.generation,
    width: (v.hosts / max) * 100,
    hostsHref: assetsHref({ product: productKey, product_version: v.version }),
    installsHref: installedHref(productKey, v.version),
    hostsLabel: `${v.version}: ${plural(v.hosts, 'host')}, open the list`,
    installsLabel: `${v.version}: ${plural(v.installs, 'installation')}, open the list`,
  }))
}

/* ── Installed on ────────────────────────────────────────────────────────── */

/** Where a software row was read from: the registry hive or package manager (one helper for every page, see model.ts). */
export { installSourceLabel } from './model'

/** The asset flyout of an installation's server: the Assets page with `?asset=server:<id>` open. */
export function installAssetHref(row: Pick<InstallationRow, 'asset_key'>): string {
  return `/inventory/assets?asset=${row.asset_key}`
}

/** "5 installations" or "3 of 19 installations" under the table caption. */
export function installedSummary(total: number, productInstalls: number, version: string | undefined): string {
  if (!version) return plural(total, 'installation')
  const label = version === NO_VERSION ? 'no version reported' : `version ${version}`
  return `${plural(total, 'installation')} with ${label} (of ${fmtInt(productInstalls)})`
}

/** The chosen version for a select: `__none__` is the rows with none. */
export function versionFilterLabel(version: string | undefined): string {
  if (!version) return 'All versions'
  return version === NO_VERSION ? 'No version reported' : version
}

export interface VersionFilterOption { value: string; label: string; installs: number | null }

/**
 * The options of the version select: all, every version (one per equivalence key, with its installations) and the
 * rows that report none, which the API addresses as `unversioned.address` (`__none__`).
 */
export function versionFilterOptions(data: Pick<ProductResponse, 'versions' | 'unversioned'> | undefined, selected: string | undefined): VersionFilterOption[] {
  const out: VersionFilterOption[] = (data?.versions ?? []).map((v) => ({ value: v.version, label: v.version, installs: v.installs }))
  if ((data?.unversioned.installs ?? 0) > 0) out.push({ value: data!.unversioned.address || NO_VERSION, label: 'No version reported', installs: data!.unversioned.installs })
  if (selected && !out.some((o) => o.value === selected)) out.unshift({ value: selected, label: versionFilterLabel(selected), installs: null })
  return out
}

/* ── Timeline ────────────────────────────────────────────────────────────── */

/** The window start, rounded down to the hour so the query key does not change on every render. */
export function timelineSince(days: number, now: number): string {
  const hour = 3_600_000
  const start = Math.floor(now / hour) * hour - days * 24 * hour
  return new Date(start).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** The Changes page filtered to one product and a chip of its statistics; `since` is the window the numbers cover. One builder: changesPageHref. */
export function productChangesHref(productKey: string, filters: { category?: 'software'; action?: 'added' | 'removed' | 'changed'; label?: 'upgrade' | 'downgrade'; since?: string } = {}): string {
  return changesPageHref({ subject_key: productKey, ...filters })
}

export interface TimelineStat { id: string; label: string; value: number; href: string }

/** The statistics of the window as links to the Changes page (each equals the total of that list). */
export function timelineStats(productKey: string, stats: { software: { added: number; removed: number; changed: number; upgraded: number; downgraded: number } } | undefined, since: string): TimelineStat[] {
  if (!stats) return []
  const s = stats.software
  const link = (id: string, label: string, value: number, f: Parameters<typeof productChangesHref>[1]): TimelineStat => ({ id, label, value, href: productChangesHref(productKey, { ...f, since }) })
  return [
    link('added', 'installed', s.added, { category: 'software', action: 'added' }),
    link('removed', 'removed', s.removed, { category: 'software', action: 'removed' }),
    link('upgraded', 'upgraded', s.upgraded, { category: 'software', action: 'changed', label: 'upgrade' }),
    link('downgraded', 'downgraded', s.downgraded, { category: 'software', action: 'changed', label: 'downgrade' }),
  ]
}

/* ── Evidence ────────────────────────────────────────────────────────────── */

/** "Showing the 200 most widely installed of 340" when the evidence list is cut short. */
export function evidenceNote(ev: ProductResponse['evidence']): string | null {
  if (!ev.truncated) return null
  return `Showing the ${fmtInt(ev.items.length)} most widely installed of ${fmtInt(ev.total)} raw names.`
}

/** How the normaliser reads a raw title today, in words. */
export function evidenceStatusText(item: Pick<EvidenceItem, 'status' | 'rule_id'>): string {
  if (!item.status) return 'Not evaluated'
  if (item.status === 'normalized') return item.rule_id ? `Rule ${item.rule_id}` : 'Matched by a rule'
  if (item.status === 'publisher_only') return 'Publisher recognised; no product rule'
  return 'No rule matched'
}

/* ── Not found ───────────────────────────────────────────────────────────── */

/** The product endpoints answer 404 for an unknown product and for one with no visible host: one state for both. */
export function isNotFound(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null | undefined)?.response?.status === 404
}
