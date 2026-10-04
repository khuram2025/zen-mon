/**
 * Pure logic shared by the breakdown cards and section pages (WP B2): whole-scope shares with an
 * explicit remainder (NF-25), stable category colours, drill-down links that keep the filter and
 * a frozen window (NF-08), ranking choices (NF-24), table sorting and paging. Free of React so
 * `scripts/test-netflow-breakdowns.mjs` can test it directly.
 */
import { FW_EVENT_FLOWS, mergeFlowFilter, removeFlowFilterKeys, type FlowFilter, type FlowFilterKey } from '@/lib/netflow/filters'
import { buildNetflowLink, hostPath } from '@/lib/netflow/links'
import { freezeWindow, type NfMetaWindow, type NfWindow } from '@/lib/netflow/window'
import type { Confidence, Conversation, DscpRow, Orientation, RankBy, RankMetric, Totals } from './shapes'

/* ─── Colours ──────────────────────────────────────────────────────── */

/**
 * Categorical palette (dataviz reference instance; validated with the six checks on this app's
 * light #ffffff and dark #0d121b card surfaces: adjacent CVD ΔE ≥ 8.4, normal-vision ΔE ≥ 19.3).
 * Three light slots sit under 3:1 against the surface, so every coloured mark is paired with a
 * legend row that names it and states its value (the relief rule). Written as literal class
 * pairs so Tailwind's scanner keeps them.
 */
export const SERIES_CLASSES = [
  'bg-[#2a78d6] dark:bg-[#3987e5]',
  'bg-[#eb6834] dark:bg-[#d95926]',
  'bg-[#1baf7a] dark:bg-[#199e70]',
  'bg-[#eda100] dark:bg-[#c98500]',
  'bg-[#e87ba4] dark:bg-[#d55181]',
  'bg-[#008300] dark:bg-[#008300]',
  'bg-[#4a3aa7] dark:bg-[#9085e9]',
  'bg-[#e34948] dark:bg-[#e66767]',
] as const

/** Neutral fill for "all other traffic": never a categorical hue. */
export const REMAINDER_CLASS = 'bg-[#a8a29e] dark:bg-[#57534e]'
/** Single-series bars (ranked hosts, interfaces): series slot 1. */
export const BAR_CLASS = SERIES_CLASSES[0]
/** Second measure beside it (received vs sent, out vs in): slot 2. */
export const BAR2_CLASS = SERIES_CLASSES[1]

/** FNV-1a: a stable 32-bit hash of a category name. */
export function hashName(name: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h >>> 0
}

/**
 * Colour slot per category name. The colour follows the entity, never its rank: a name hashes
 * to a slot, so "DNS" keeps its colour when the order or the other slices change. When two
 * shown names hash to the same slot, the alphabetically later one takes the next free slot, so
 * the result depends only on which names are shown, not on their order.
 */
export function assignColorSlots(names: readonly string[], slots = SERIES_CLASSES.length): Map<string, number> {
  const out = new Map<string, number>()
  const used = new Set<number>()
  const unique = Array.from(new Set(names)).sort((a, b) => a.localeCompare(b))
  for (const name of unique) {
    let slot = hashName(name) % slots
    for (let i = 0; i < slots && used.has(slot); i++) slot = (slot + 1) % slots
    if (used.size >= slots) slot = hashName(name) % slots // more names than slots: reuse
    used.add(slot)
    out.set(name, slot)
  }
  return out
}

export function seriesClass(slot: number | undefined): string {
  return slot === undefined ? REMAINDER_CLASS : SERIES_CLASSES[((slot % SERIES_CLASSES.length) + SERIES_CLASSES.length) % SERIES_CLASSES.length]
}

/* ─── Shares and remainders (NF-25) ────────────────────────────────── */

export interface ShareRow<T = unknown> {
  key: string
  label: string
  value: number
  /** Percent of the denominator (0..100), null when the denominator is unknown or zero. */
  pct: number | null
  slot?: number
  item?: T
  remainder?: boolean
}

export interface ShareResult<T> {
  rows: ShareRow<T>[]
  /** Everything the listed rows leave of the denominator (null when nothing is left). */
  remainder: ShareRow<T> | null
  denominator: number
  /** 'scope': the whole filtered scope; 'subtotal': only the rows the response carried. */
  basis: 'scope' | 'subtotal'
}

export interface ShareOptions<T> {
  key: (item: T) => string
  label: (item: T) => string
  value: (item: T) => number
  /** Whole-scope denominator; without it the shares are of the listed subtotal (and say so). */
  total?: number | null
  /** Rows listed individually; the rest fold into the remainder. */
  max?: number
  remainderLabel?: string
  /** Assign category colours (additive breakdowns). */
  colors?: boolean
}

/**
 * Shares of an additive breakdown against the whole scope, with an explicit remainder so the
 * listed rows plus "all other" always reconcile with the stated denominator.
 */
export function shareRows<T>(items: readonly T[], opts: ShareOptions<T>): ShareResult<T> {
  const max = opts.max ?? items.length
  const listed = items.slice(0, max)
  const listedSum = listed.reduce((s, it) => s + Math.max(0, opts.value(it)), 0)
  const allSum = items.reduce((s, it) => s + Math.max(0, opts.value(it)), 0)
  const scope = typeof opts.total === 'number' && Number.isFinite(opts.total) && opts.total >= allSum
  const denominator = scope ? (opts.total as number) : allSum
  const pct = (v: number) => (denominator > 0 ? (100 * v) / denominator : null)
  const slots = opts.colors ? assignColorSlots(listed.map(opts.key)) : null
  const rows: ShareRow<T>[] = listed.map((it) => {
    const key = opts.key(it)
    const value = Math.max(0, opts.value(it))
    return { key, label: opts.label(it), value, pct: pct(value), slot: slots?.get(key), item: it }
  })
  const rest = denominator - listedSum
  const remainder: ShareRow<T> | null =
    rest > 0
      ? { key: '__remainder__', label: opts.remainderLabel ?? 'All other traffic', value: rest, pct: pct(rest), remainder: true }
      : null
  return { rows, remainder, denominator, basis: scope ? 'scope' : 'subtotal' }
}

export interface RankRow<T = unknown> {
  key: string
  label: string
  value: number
  /** Share of the scope total (non-additive rankings: each row on its own). */
  pct: number | null
  /** Bar length relative to the largest row (0..100). */
  width: number
  item: T
}

/**
 * Ranked bars for non-additive rankings (endpoint involvement, interfaces, per-side countries):
 * each row states its own share of the scope, and bar lengths compare rows with each other.
 */
export function rankRows<T>(items: readonly T[], opts: { key: (i: T) => string; label: (i: T) => string; value: (i: T) => number; total?: number | null }): RankRow<T>[] {
  const maxV = items.reduce((m, it) => Math.max(m, opts.value(it)), 0)
  const total = typeof opts.total === 'number' && opts.total > 0 ? opts.total : null
  return items.map((it) => {
    const value = Math.max(0, opts.value(it))
    return {
      key: opts.key(it),
      label: opts.label(it),
      value,
      pct: total ? (100 * value) / total : null,
      width: maxV > 0 ? (100 * value) / maxV : 0,
      item: it,
    }
  })
}

/** Remainder check used by tests and the UI: listed + remainder = denominator. */
export function reconciles(result: ShareResult<unknown>): boolean {
  const sum = result.rows.reduce((s, r) => s + r.value, 0) + (result.remainder?.value ?? 0)
  return Math.abs(sum - result.denominator) < 0.5
}

/* ─── DSCP: records without ToS (principle 1/6) ───────────────────── */

/** A DSCP share row, or the bucket of records whose exporter did not send the ToS field. */
export type DscpShareItem = DscpRow & { notExported?: boolean }

/**
 * DSCP rows plus a "Not exported" bucket for traffic whose ToS field was not exported: it is
 * unknown, never DSCP 0 / Best Effort, and cannot be filtered. Placed by size, and never beyond the
 * `max` rows a card lists, so it is not folded into "Other classes".
 */
export function dscpShareItems(page: { items: DscpRow[]; not_exported?: Pick<DscpRow, 'bytes' | 'packets' | 'flows'> | null }, max?: number): DscpShareItem[] {
  const ne = page.not_exported
  const items: DscpShareItem[] = [...page.items]
  if (!ne || !(ne.bytes > 0 || ne.flows > 0)) return items
  const row: DscpShareItem = { dscp: -1, label: 'Not exported', bytes: ne.bytes, packets: ne.packets, flows: ne.flows, selected: false, notExported: true }
  let at = items.findIndex((x) => x.bytes < row.bytes)
  if (at < 0) at = items.length
  if (max !== undefined && max > 0) at = Math.min(at, max - 1)
  items.splice(at, 0, row)
  return items
}

/* ─── Ranking (NF-24) ──────────────────────────────────────────────── */

export const RANK_BY_OPTIONS: { value: RankBy; label: string; hint: string }[] = [
  { value: 'src', label: 'Sent', hint: 'Ranked by bytes the host sent (it was the source)' },
  { value: 'dst', label: 'Received', hint: 'Ranked by bytes the host received (it was the destination)' },
  { value: 'total', label: 'Total', hint: 'Ranked by all traffic the host took part in (hosts overlap)' },
]

export const RANK_METRIC_OPTIONS: { value: RankMetric; label: string }[] = [
  { value: 'bytes', label: 'Bytes' },
  { value: 'packets', label: 'Packets' },
  { value: 'flows', label: 'Flows' },
]

export function parseRankBy(value: string | null | undefined, fallback: RankBy): RankBy {
  return value === 'src' || value === 'dst' || value === 'total' ? value : fallback
}

export function parseRankMetric(value: string | null | undefined, fallback: RankMetric = 'bytes'): RankMetric {
  return value === 'bytes' || value === 'packets' || value === 'flows' ? value : fallback
}

/** Sections and the ranking they open with: Top Talkers ranks senders, Endpoints involvement. */
export function defaultRankBy(section: string): RankBy {
  return section === 'top-talkers' ? 'src' : 'total'
}

/** "Ranked by bytes sent" — the metric the table and details emphasise. */
export function rankPhrase(by: RankBy, metric: RankMetric = 'bytes'): string {
  const what = metric === 'bytes' ? 'bytes' : metric
  if (by === 'src') return `Ranked by ${what} sent`
  if (by === 'dst') return `Ranked by ${what} received`
  return `Ranked by all ${what} the host took part in`
}

/** Which field of a talker row carries the ranked value. */
export function rankField(by: RankBy, metric: RankMetric): string {
  return by === 'total' ? metric : `${by}_${metric}`
}

/* ─── Drill-down links (NF-08) ─────────────────────────────────────── */

export type FilterPatch = Partial<Record<FlowFilterKey, unknown>>

/** The page filter narrowed to one item (numeric zero kept; `undefined` removes a key). */
export function drillFilter(base: FlowFilter, patch: FilterPatch): FlowFilter {
  return mergeFlowFilter(base, patch)
}

/**
 * Evidence link into Forensics: the page filter plus the item, with the window frozen to the
 * exact bounds of the data on screen (`meta.window`), so the raw records match the number that
 * was clicked even after the relative window has moved on. Cards count flow records only, so the
 * link asks for those (`fw_event=flows`) unless the page already filters by a firewall event: NSEL
 * connection-created/denied events (no volume) would otherwise inflate the drill-down silently.
 */
export function forensicsLink(base: FlowFilter, patch: FilterPatch, w: NfWindow, meta?: Pick<NfMetaWindow, 'from' | 'to'> | null): string {
  const filter = drillFilter(base, patch)
  return buildNetflowLink('/netflow/forensics', {
    filter: filter.fw_event === undefined ? { ...filter, fw_event: FW_EVENT_FLOWS } : filter,
    window: freezeWindow(w, meta),
    extra: { run: true },
  })
}

/** Keys that describe one host; a host profile does not inherit another host's filter. */
const HOST_KEYS: FlowFilterKey[] = ['talker', 'src', 'dst', 'not_talker', 'not_src', 'not_dst']

/** Host profile (B7's page): the page context without host-specific keys, window frozen. */
export function hostLink(ip: string, base: FlowFilter, w: NfWindow, meta?: Pick<NfMetaWindow, 'from' | 'to'> | null): string {
  return buildNetflowLink(hostPath(ip), { filter: removeFlowFilterKeys(base, HOST_KEYS), window: freezeWindow(w, meta) })
}

/** A section page ("View all") with the live page window and filter plus section extras. */
export function sectionLink(section: string, base: FlowFilter, w: NfWindow, extra?: Record<string, string | number | null | undefined>, exporter?: string): string {
  const filter = exporter ? drillFilter(base, { exporter }) : base
  return buildNetflowLink(`/netflow/${section}`, { filter, window: w, extra })
}

/**
 * The two halves of a conversation as exact Forensics queries (a pair filter has no
 * "either direction" form, so each direction is its own link): client → server requests and
 * server → client responses, on the conversation's protocol and service.
 */
export function conversationLinks(row: Pick<Conversation, 'client' | 'server' | 'protocol' | 'drill' | 'orientation'>, base: FlowFilter, w: NfWindow, meta?: Pick<NfMetaWindow, 'from' | 'to'> | null) {
  const svc = { ...row.drill, protocol: row.protocol }
  // A service-port drill matches both halves; the direction comes from src/dst.
  const clean = removeFlowFilterKeys(base, ['talker', 'src', 'dst'])
  return {
    forward: forensicsLink(clean, { ...svc, src: row.client, dst: row.server }, w, meta),
    reverse: forensicsLink(clean, { ...svc, src: row.server, dst: row.client }, w, meta),
    labels: orientationLabels(row.orientation),
  }
}

export function orientationLabels(o: Orientation): { a: string; b: string; forward: string; reverse: string } {
  if (o === 'client_server') return { a: 'Client', b: 'Server', forward: 'Client → server', reverse: 'Server → client' }
  return { a: 'Endpoint A', b: 'Endpoint B', forward: 'A → B', reverse: 'B → A' }
}

/* ─── Unrecorded addresses ─────────────────────────────────────────── */

/**
 * The unspecified address. The collector no longer writes it (contract §4.1), but records stored
 * before the IPv6 upgrade carry 0.0.0.0 for IPv6 flows. It is shown as "address not recorded",
 * never as a host: no profile link, and its traffic still counts in the scope totals.
 */
export function isUnspecifiedIp(ip: string | null | undefined): boolean {
  if (!ip) return false
  const v = ip.trim().toLowerCase()
  return v === '0.0.0.0' || v === '::' || v === '::ffff:0.0.0.0' || v === '0:0:0:0:0:0:0:0'
}

export const UNRECORDED_ADDRESS_LABEL = 'Address not recorded'
export const UNRECORDED_ADDRESS_HINT =
  'Legacy records: before the IPv6 upgrade the collector stored IPv6 flows without their addresses (0.0.0.0).'

/* ─── Classification labels (NF-20) ────────────────────────────────── */

export const CONFIDENCE_LABELS: Record<Confidence, string> = {
  high: 'High confidence',
  medium: 'Medium confidence',
  low: 'Low confidence (guess)',
  'n/a': 'Not classified',
}

/** "HTTPS 443/tcp", "Echo request (ICMP)", "Not applicable (ESP)". */
export function serviceText(row: { service: string | null; service_note?: string | null; service_port: number | null; protocol: number; protocol_name?: string; icmp_type_code?: number; port_applicable: boolean }): string {
  const proto = row.protocol_name ?? `protocol ${row.protocol}`
  if (row.icmp_type_code !== undefined && row.icmp_type_code !== null) return `${row.service ?? `Type ${row.icmp_type_code >> 8}`} (${proto})`
  if (!row.port_applicable) return `Not applicable (${proto})`
  if (row.service_port) return `${row.service ?? row.service_port} ${row.service_port}/${proto.toLowerCase()}`
  return row.service_note ?? 'Unidentified'
}

/* ─── Tables: sorting and paging ───────────────────────────────────── */

export type SortDir = 'asc' | 'desc'

export function sortRowsBy<T>(rows: readonly T[], value: (row: T) => number | string | null | undefined, dir: SortDir): T[] {
  const mult = dir === 'asc' ? 1 : -1
  return rows
    .map((row, i) => ({ row, i, v: value(row) }))
    .sort((a, b) => {
      const av = a.v
      const bv = b.v
      if (av === bv) return a.i - b.i
      if (av === null || av === undefined) return 1
      if (bv === null || bv === undefined) return -1
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * mult
      return String(av).localeCompare(String(bv), undefined, { numeric: true }) * mult
    })
    .map((x) => x.row)
}

export const PAGE_SIZE = 50

export function pageCount(total: number | null | undefined, size = PAGE_SIZE): number {
  return total && total > 0 ? Math.ceil(total / size) : 1
}

export function pageSlice<T>(rows: readonly T[], page: number, size = PAGE_SIZE): T[] {
  return rows.slice(page * size, page * size + size)
}

/** "Showing 51–100 of 1,564". */
export function showingText(offset: number, returned: number, total: number | null | undefined): string {
  if (returned === 0) return total ? `Nothing on this page of ${total.toLocaleString('en-US')}` : 'Nothing to show'
  const from = (offset + 1).toLocaleString('en-US')
  const to = (offset + returned).toLocaleString('en-US')
  return total != null ? `Showing ${from}–${to} of ${total.toLocaleString('en-US')}` : `Showing ${from}–${to}`
}

/** Clamp a page number from the URL. */
export function parsePage(value: string | null | undefined): number {
  const n = Number(value)
  return Number.isInteger(n) && n >= 1 && n <= 100000 ? n - 1 : 0
}

/* ─── Totals helpers ───────────────────────────────────────────────── */

export function totalOf(t: Totals | null | undefined, metric: RankMetric): number | null {
  return t ? t[metric] : null
}

/** Sent/received split of a byte count (0..100 each), null when there is nothing. */
export function splitPct(a: number, b: number): { a: number; b: number } | null {
  const sum = a + b
  if (!(sum > 0)) return null
  return { a: (100 * a) / sum, b: (100 * b) / sum }
}
