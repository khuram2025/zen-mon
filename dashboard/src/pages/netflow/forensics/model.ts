/**
 * Forensics and saved-views page logic (NF-08, NF-09, NF-10, NF-11, NF-12, NF-34, FE-04, FE-10), kept
 * free of React so it can be tested on its own (scripts/test-netflow-forensics.mjs).
 *
 * The URL is the single source of truth for the *applied* search: filter keys (contract §8), the
 * window (`range`/`from`/`to`), `sort`/`order`, the chosen `columns`, `run=1` (search even without
 * filters) and, for evidence links, `finding`/`finding_id`. The form holds a *draft* that is only
 * applied on Search (or Enter), so typing never runs a query.
 */
import {
  FLOW_FILTER_ALIASES,
  FLOW_FILTER_KEYS,
  flowFilterFromDraft,
  flowFilterToApiParams,
  flowFilterToDraft,
  FW_EVENT_FLOWS,
  isFlowFilterEmpty,
  migrateSavedQuery,
  parseFlowFilterDetailed,
  removeFlowFilterKeys,
  writeFlowFilter,
  type FlowFilter,
  type FlowFilterDraft,
  type FlowFilterErrors,
  type FlowFilterKey,
  type SavedQueryV2,
  type SavedQueryWindow,
  type SavedViewKind,
} from '@/lib/netflow/filters'
import { formatCountExact, formatDateTime, ipDisplay, protocolName } from '@/lib/netflow/format'
import { savedQueryLink, buildNetflowLink } from '@/lib/netflow/links'
import type { NfMeta } from '@/lib/netflow/types'
import type { NfWindow } from '@/lib/netflow/window'

/* ─── API shapes ───────────────────────────────────────────────────── */

export interface ForensicsInterface {
  exporter_ip: string
  ifindex: number
  display_name?: string | null
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  device_hostname?: string | null
}

export interface ForensicsRow {
  id: string
  timestamp: string | null
  received_at: string | null
  exporter: string
  observation_domain: number
  export_protocol: string | null
  flow_version: number
  collector_id?: string | null
  ip_version: number
  src: string
  dst: string
  src_port: number | null
  dst_port: number | null
  protocol: number
  protocol_name: string
  /** ICMP / ICMPv6 type and code (protocol 1/58: stored as type × 256 + code in dst_port, never ports). */
  icmp_type?: number | null
  icmp_code?: number | null
  service: string | null
  service_port: number | null
  application: string | null
  class_direction: string | null
  class_basis: string | null
  tcp_flags: number | null
  flags_state: 'exported' | 'absent' | 'legacy' | 'n/a'
  tos: number | null
  dscp: number | null
  input_if: number | null
  output_if: number | null
  input_interface: ForensicsInterface | null
  output_interface: ForensicsInterface | null
  packets: number
  bytes: number
  sampling_interval: number
  duration_ms: number | null
  src_as: number | null
  dst_as: number | null
  src_mask: number | null
  dst_mask: number | null
  next_hop: string | null
  flow_direction: 'ingress' | 'egress' | null
  app_id: number | null
  fw_event: string | null
  nat: { src: string | null; src_port: number | null; dst: string | null; dst_port: number | null } | null
  flow_end_reason: number | null
  fields_present: number
  tracked: boolean
  clock_clamped: boolean
  reverse: boolean
}

export interface ForensicsUnavailable {
  reason: 'raw_retention' | string
  raw_days: number | null
  earliest_raw: string | null
  message: string
}

export interface ForensicsPageData {
  items: ForensicsRow[]
  page_size: number
  has_more: boolean
  next_cursor: string | null
  total: number | null
  total_exact: boolean
  total_bytes: number | null
  total_packets: number | null
  offset: number
  sort: string
  order: 'asc' | 'desc'
  snapshot_at: string | null
  /** The received-at snapshot in epoch ms: exports pass it back so the file holds exactly the counted rows. */
  snapshot_ms?: number | null
  /** NSEL connection-created / denied event records among the matches (no traffic volume; volume totals leave them out). */
  fw_events?: { created: number; denied: number; exact: boolean } | null
  window: { from: string; to: string; mode: string; tz: string; effective_from: string | null }
  unavailable: ForensicsUnavailable | null
  export?: { stream_max_rows: number; job_max_rows: number; allowed: boolean }
}

export interface ExportJob {
  id: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'expired'
  requested_by_username: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  expires_at: string | null
  row_count: number | null
  byte_size: number | null
  total: number | null
  total_exact: boolean | null
  progress_pct: number | null
  error: string | null
  download_ready: boolean
  filename: string | null
  query: { filter?: Record<string, string>; window?: { from: string; to: string; tz?: string }; sort?: string; order?: string }
}

export interface ExportJobList {
  items: ExportJob[]
  limits: { stream_max_rows: number; job_max_rows: number; retention_hours: number; active_per_user: number }
  allowed: boolean
}

export interface SavedView {
  id: string
  name: string
  description: string | null
  kind: SavedViewKind
  visibility: 'private' | 'shared'
  pinned: boolean
  query: unknown
  query_version: number
  query_issues: { key: string; value: string; reason: string }[]
  legacy_time: boolean
  owner_id: string | null
  owner_username: string | null
  is_owner: boolean
  can_edit: boolean
  can_delete: boolean
  can_change_visibility: boolean
  updated_by_username: string | null
  created_at: string | null
  updated_at: string | null
}

/* ─── Sort, order, columns ─────────────────────────────────────────── */

export const SORTS = ['timestamp', 'bytes', 'packets', 'src_port', 'dst_port', 'duration'] as const
export type ForensicsSort = (typeof SORTS)[number]
export type SortOrder = 'asc' | 'desc'

export const SORT_LABELS: Record<ForensicsSort, string> = {
  timestamp: 'Flow end time',
  bytes: 'Bytes',
  packets: 'Packets',
  src_port: 'Source port',
  dst_port: 'Destination port',
  duration: 'Duration',
}

export interface ColumnDef {
  key: string
  label: string
  /** Shown when the URL names no columns. */
  default: boolean
  sort?: ForensicsSort
  numeric?: boolean
  /** CSV header for "export displayed rows" (the server export's names). */
  csv: string[]
}

/** Result columns; the chooser toggles them and saved views remember the choice. */
export const COLUMNS: ColumnDef[] = [
  { key: 'time', label: 'Flow end', default: true, sort: 'timestamp', csv: ['timestamp_utc'] },
  { key: 'exporter', label: 'Exporter', default: true, csv: ['exporter'] },
  { key: 'src', label: 'Source', default: true, csv: ['src', 'src_port'] },
  { key: 'dst', label: 'Destination', default: true, csv: ['dst', 'dst_port'] },
  { key: 'protocol', label: 'Protocol', default: true, csv: ['protocol', 'protocol_name', 'icmp_type', 'icmp_code'] },
  { key: 'service', label: 'Service', default: true, csv: ['service', 'application'] },
  { key: 'flags', label: 'TCP flags', default: true, csv: ['tcp_flags'] },
  { key: 'dscp', label: 'DSCP', default: false, csv: ['dscp'] },
  { key: 'interfaces', label: 'Interfaces in/out', default: true, csv: ['input_if', 'input_interface', 'output_if', 'output_interface'] },
  { key: 'bytes', label: 'Bytes', default: true, sort: 'bytes', numeric: true, csv: ['bytes'] },
  { key: 'packets', label: 'Packets', default: true, sort: 'packets', numeric: true, csv: ['packets'] },
  { key: 'duration', label: 'Duration', default: true, sort: 'duration', numeric: true, csv: ['duration_ms'] },
  { key: 'sampling', label: 'Sampling', default: false, numeric: true, csv: ['sampling_interval'] },
  { key: 'direction', label: 'Flow direction', default: false, csv: ['flow_direction'] },
  { key: 'as', label: 'AS (src → dst)', default: false, csv: ['src_as', 'dst_as'] },
  { key: 'next_hop', label: 'Next hop', default: false, csv: ['next_hop'] },
  { key: 'nat', label: 'NAT (post-translation)', default: false, csv: ['nat_src', 'nat_src_port', 'nat_dst', 'nat_dst_port'] },
  { key: 'fw_event', label: 'Firewall event', default: false, csv: ['fw_event'] },
  { key: 'received', label: 'Received', default: false, csv: ['received_at_utc'] },
]
export const COLUMN_KEYS = COLUMNS.map((c) => c.key)
export const DEFAULT_COLUMNS = COLUMNS.filter((c) => c.default).map((c) => c.key)

export function parseSort(value: string | null | undefined): ForensicsSort {
  return (SORTS as readonly string[]).includes(value ?? '') ? (value as ForensicsSort) : 'timestamp'
}

export function parseOrder(value: string | null | undefined): SortOrder {
  return value === 'asc' ? 'asc' : 'desc'
}

/** Known columns from the URL, in canonical order; the defaults when absent or empty. */
export function parseColumns(value: string | null | undefined): string[] {
  if (!value) return [...DEFAULT_COLUMNS]
  const wanted = new Set(value.split(',').map((c) => c.trim()))
  const cols = COLUMN_KEYS.filter((k) => wanted.has(k))
  return cols.length ? cols : [...DEFAULT_COLUMNS]
}

export function isDefaultColumns(cols: readonly string[]): boolean {
  return cols.length === DEFAULT_COLUMNS.length && cols.every((c, i) => c === DEFAULT_COLUMNS[i])
}

/* ─── Applied search (URL) ─────────────────────────────────────────── */

/**
 * Evidence-link parameters. Anomalies links send `finding=<finding id>&finding_kind=<detector>`;
 * older links send `finding=<detector>&finding_id=<id>`. Both are understood.
 */
export const EVIDENCE_PARAMS = ['finding', 'finding_id', 'finding_kind'] as const
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Detector kind and finding id of an evidence link, whichever convention it uses. */
export function evidenceFromUrl(sp: URLSearchParams): { finding: string | null; findingId: string | null } {
  const raw = sp.get('finding') || null
  const rawIsId = raw !== null && UUID_RE.test(raw)
  return {
    finding: sp.get('finding_kind') || (rawIsId ? null : raw),
    findingId: sp.get('finding_id') || (rawIsId ? raw : null),
  }
}
/** Parameters Reset removes (the window, `tz` and the column choice stay). */
export const QUERY_PARAMS = ['sort', 'order', 'run', 'cursor', 'view', ...EVIDENCE_PARAMS] as const

export interface AppliedSearch {
  filter: FlowFilter
  errors: FlowFilterErrors
  invalid: FlowFilterDraft
  sort: ForensicsSort
  order: SortOrder
  columns: string[]
  run: boolean
  finding: string | null
  findingId: string | null
  viewId: string | null
}

export function appliedSearchFromUrl(sp: URLSearchParams): AppliedSearch {
  const parsed = parseFlowFilterDetailed(sp)
  return {
    filter: parsed.filter,
    errors: parsed.errors,
    invalid: parsed.invalid,
    sort: parseSort(sp.get('sort')),
    order: parseOrder(sp.get('order')),
    columns: parseColumns(sp.get('columns')),
    run: sp.get('run') === '1',
    ...evidenceFromUrl(sp),
    viewId: sp.get('view') || null,
  }
}

/** Keys whose URL value was rejected: the search does not run until they are fixed or removed. */
export function invalidKeys(a: Pick<AppliedSearch, 'invalid' | 'errors'>): FlowFilterKey[] {
  return FLOW_FILTER_KEYS.filter((k) => a.invalid[k] !== undefined || (a.errors[k] !== undefined && k !== 'tz'))
}

/**
 * Whether the applied search runs: any filter (numeric zero counts) or an explicit `run=1` — and
 * nothing invalid, which would otherwise be silently left out of the query. A finding reference
 * alone does not run anything: evidence links carry the detector's filters and `run=1`, and a bare,
 * stale or unknown finding id must never turn into an unfiltered search shown as "evidence".
 */
export function isRunnable(a: AppliedSearch): boolean {
  if (invalidKeys(a).length) return false
  return a.run || !isFlowFilterEmpty(a.filter)
}

/** How far the finding behind an evidence link could be confirmed (EvidenceBanner). */
export type EvidenceState = 'loading' | 'found' | 'missing' | 'unverified'

/**
 * `found`: the finding loaded. `missing`: the id is malformed, or the API answered 404/403 (deleted,
 * never existed, or outside the caller's scope). `unverified`: no id was given (an older link with
 * only the detector kind) or the lookup failed for another reason.
 */
export function evidenceState(opts: { findingId: string | null; loading: boolean; found: boolean; status: number | null }): EvidenceState {
  if (opts.findingId !== null && !UUID_RE.test(opts.findingId)) return 'missing'
  if (opts.findingId === null) return 'unverified'
  if (opts.found) return 'found'
  if (opts.loading) return 'loading'
  return opts.status === 404 || opts.status === 403 ? 'missing' : 'unverified'
}

/** The URL for a submitted draft filter: applied filter, sort/order kept, `run=1`, no cursor. */
export function submitSearchParams(sp: URLSearchParams, filter: FlowFilter, opts: { keepEvidence?: boolean } = {}): URLSearchParams {
  const next = writeFlowFilter(sp, filter)
  next.set('run', '1')
  next.delete('cursor')
  if (!opts.keepEvidence) for (const k of EVIDENCE_PARAMS) next.delete(k)
  return next
}

/**
 * Whether two URLs hold the same search regardless of parameter order: submitting an unchanged
 * form refreshes the results instead of pushing a duplicate history entry (Back stays useful).
 */
export function sameSearchParams(a: URLSearchParams, b: URLSearchParams): boolean {
  const canon = (sp: URLSearchParams) =>
    [...sp.entries()]
      .filter(([, v]) => v !== '')
      .map(([k, v]) => `${k}=${v}`)
      .sort()
      .join('&')
  return canon(a) === canon(b)
}

/** Reset: every filter key and alias, sort/order, run, evidence and view parameters removed. */
export function resetSearchParams(sp: URLSearchParams): URLSearchParams {
  const next = writeFlowFilter(sp, {})
  for (const k of QUERY_PARAMS) next.delete(k)
  // `tz` is a window setting, not a filter: keep it.
  if (sp.get('tz')) next.set('tz', sp.get('tz') as string)
  return next
}

export function sortSearchParams(sp: URLSearchParams, sort: ForensicsSort, order: SortOrder): URLSearchParams {
  const next = new URLSearchParams(sp)
  if (sort === 'timestamp' && order === 'desc') {
    next.delete('sort')
    next.delete('order')
  } else {
    next.set('sort', sort)
    next.set('order', order)
  }
  next.delete('cursor')
  return next
}

/** Clicking a sortable header: same column flips the order, a new column starts descending. */
export function nextSort(current: { sort: ForensicsSort; order: SortOrder }, col: ForensicsSort): { sort: ForensicsSort; order: SortOrder } {
  if (current.sort === col) return { sort: col, order: current.order === 'desc' ? 'asc' : 'desc' }
  return { sort: col, order: 'desc' }
}

export function columnsSearchParams(sp: URLSearchParams, cols: string[]): URLSearchParams {
  const next = new URLSearchParams(sp)
  const ordered = COLUMN_KEYS.filter((k) => cols.includes(k))
  if (!ordered.length || isDefaultColumns(ordered)) next.delete('columns')
  else next.set('columns', ordered.join(','))
  return next
}

/** API parameters for the search (window params come from `windowParams`). */
export function searchApiParams(filter: FlowFilter, sort: ForensicsSort, order: SortOrder, pageSize = 500): Record<string, string> {
  return { ...flowFilterToApiParams(filter), sort, order, page_size: String(pageSize) }
}

/* ─── Draft ────────────────────────────────────────────────────────── */

export type DraftState = Record<FlowFilterKey, string>

/** The form text for the applied filter; rejected URL values are shown as typed so they can be fixed. */
export function draftFromApplied(a: Pick<AppliedSearch, 'filter' | 'invalid'>): DraftState {
  const d = flowFilterToDraft(a.filter)
  for (const k of FLOW_FILTER_KEYS) {
    const raw = a.invalid[k]
    if (raw !== undefined) d[k] = raw
  }
  return d
}

const PROTOCOL_BY_NAME: Record<string, number> = {
  icmp: 1, igmp: 2, tcp: 6, udp: 17, gre: 47, esp: 50, ah: 51, icmpv6: 58, 'ipv6-icmp': 58, eigrp: 88,
  ospf: 89, pim: 103, vrrp: 112, l2tp: 115, sctp: 132,
}

/** "tcp", "TCP (6)" and "6" all mean protocol 6; anything else is left for validation to explain. */
export function normalizeProtocolText(text: string): string {
  const t = text.trim()
  if (!t) return ''
  const paren = /\((\d{1,3})\)\s*$/.exec(t)
  if (paren) return paren[1]
  const byName = PROTOCOL_BY_NAME[t.toLowerCase()]
  return byName !== undefined ? String(byName) : t
}

export interface DraftResult {
  filter: FlowFilter
  errors: FlowFilterErrors
  ok: boolean
}

/** Validate the draft locally with the URL rules; the API is never asked to reject what we can. */
export function parseDraft(draft: DraftState): DraftResult {
  const clean: FlowFilterDraft = {}
  for (const k of FLOW_FILTER_KEYS) {
    const v = (draft[k] ?? '').trim()
    if (v !== '') clean[k] = k === 'protocol' ? normalizeProtocolText(v) : v
  }
  // A direction without an interface is meaningless in the form; ignore 'any'.
  if (clean.iface_dir === 'any') delete clean.iface_dir
  const parsed = flowFilterFromDraft(clean)
  return { filter: parsed.filter, errors: parsed.errors, ok: Object.keys(parsed.errors).length === 0 }
}

export function draftsEqual(a: DraftState, b: DraftState): boolean {
  return FLOW_FILTER_KEYS.every((k) => (a[k] ?? '').trim() === (b[k] ?? '').trim())
}

/** Common protocols for the protocol input's suggestions; any 0–255 value is accepted. */
export const PROTOCOL_SUGGESTIONS: { value: number; label: string }[] = [6, 17, 1, 58, 47, 50, 51, 89, 112, 132].map((n) => ({
  value: n,
  label: protocolName(n) ?? `Protocol ${n}`,
}))

/** Network classes (same names the API's `netclass` filter accepts). */
export const NET_CLASS_OPTIONS = [
  'Private 10/8', 'Private 172.16/12', 'Private 192.168/16', 'CGNAT', 'Loopback', 'Link-local', 'Multicast',
  'Unique local (IPv6)', 'Documentation / special-use', 'Public',
] as const

/* ─── Results labels ───────────────────────────────────────────────── */

/**
 * The results heading. While a new search loads with the previous results still on screen
 * (`placeholder`), and after a search failed, the previous search's count is never shown under the
 * new filter chips.
 */
export function searchHeading(o: { page: ForensicsPageData | null | undefined; placeholder: boolean; failed: boolean; loaded: number }): string {
  if (o.placeholder) return 'Searching…'
  if (!o.page) return o.failed ? 'Search failed' : 'Searching…'
  if (o.page.unavailable) return 'Raw records unavailable for this period'
  return showingLabel(o.loaded, o.page.total, o.page.total_exact)
}

/** NSEL connection-created/denied event records counted in this search (0 when none or unknown). */
export function firewallEventCount(page: Pick<ForensicsPageData, 'fw_events'> | null | undefined): number {
  const e = page?.fw_events
  return e ? (e.created || 0) + (e.denied || 0) : 0
}

/** The same search limited to flow records (`fw_event=flows`): what volume totals and cards count. */
export function flowRecordsOnlyParams(sp: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(sp)
  next.set('fw_event', FW_EVENT_FLOWS)
  for (const k of [...EVIDENCE_PARAMS, 'cursor']) next.delete(k)
  next.set('run', '1')
  return next
}

/**
 * API parameters of "All matching rows" exports: the applied search pinned to the page's window
 * *and* its received-at snapshot, so the file holds exactly the rows the page counted — never the
 * records that arrived (with timestamps inside the window) after the search ran.
 */
export function exportApiParams(opts: {
  filter: FlowFilter
  sort: ForensicsSort
  order: SortOrder
  page: Pick<ForensicsPageData, 'window' | 'snapshot_at' | 'snapshot_ms'> | null | undefined
  windowParams: Record<string, string>
  tz: string
}): URLSearchParams {
  const p = new URLSearchParams(searchApiParams(opts.filter, opts.sort, opts.order))
  p.delete('page_size')
  if (opts.page?.window) {
    p.set('from', opts.page.window.from)
    p.set('to', opts.page.window.to)
  } else {
    for (const [k, v] of Object.entries(opts.windowParams)) p.set(k, v)
  }
  const snap = opts.page?.snapshot_ms ?? opts.page?.snapshot_at ?? null
  if (snap !== null && snap !== undefined && snap !== '') p.set('snapshot', String(snap))
  p.set('tz', opts.tz)
  return p
}

/**
 * Header links of a Forensics page that keep the investigation context (filter and window): the
 * root "NetFlow" crumb opens the overview with them, and an exporter-scoped search gets its
 * exporter page as a parent crumb.
 */
export function forensicsHeaderLinks(filter: FlowFilter, w: NfWindow): { root: string; parents: { label: string; to: string }[] } {
  const parents = filter.exporter
    ? [{
        label: `Exporter ${ipDisplay(filter.exporter)}`,
        to: buildNetflowLink(`/netflow/devices/${encodeURIComponent(filter.exporter)}`, { filter: removeFlowFilterKeys(filter, ['exporter']), window: w }),
      }]
    : []
  return { root: buildNetflowLink('/netflow', { filter, window: w }), parents }
}

/** ICMP / ICMPv6 record: its "ports" are the encoded type and code (contract §3.1), not ports. */
export function isIcmpRow(row: Pick<ForensicsRow, 'protocol'>): boolean {
  return row.protocol === 1 || row.protocol === 58
}

/** `ip:port`, `[v6]:port`, or the bare address when there is no port. */
export function endpointText(ip: string, port: number | null | undefined): string {
  const shown = ipDisplay(ip)
  if (port === null || port === undefined) return shown
  return shown.includes(':') ? `[${shown}]:${port}` : `${shown}:${port}`
}

/** One side of a record's endpoints: the address alone for ICMP (no `:2048` "port"). */
export function rowEndpoint(row: Pick<ForensicsRow, 'protocol' | 'src' | 'dst' | 'src_port' | 'dst_port'>, side: 'src' | 'dst'): string {
  const ip = side === 'src' ? row.src : row.dst
  if (isIcmpRow(row)) return endpointText(ip, null)
  return endpointText(ip, side === 'src' ? row.src_port : row.dst_port)
}

/** "type 8 code 0" for ICMP records (from the API's icmp_type/icmp_code, else decoded from dst_port). */
export function icmpTypeCodeText(row: Pick<ForensicsRow, 'protocol' | 'dst_port' | 'icmp_type' | 'icmp_code'>): string | null {
  if (!isIcmpRow(row)) return null
  const raw = row.dst_port
  const type = row.icmp_type ?? (raw !== null && raw !== undefined ? (raw >> 8) & 0xff : null)
  const code = row.icmp_code ?? (raw !== null && raw !== undefined ? raw & 0xff : null)
  if (type === null || code === null) return null
  return `type ${type} code ${code}`
}

/** "Showing 1–500 of 49,212", "Showing 1–500 of at least 120,000", "No records matched". */
export function showingLabel(loaded: number, total: number | null, exact: boolean): string {
  if (loaded === 0) return 'No records matched'
  const range = `Showing 1–${formatCountExact(loaded)}`
  if (total === null || total === undefined) return `${range} (total unknown)`
  if (!exact) return `${range} of at least ${formatCountExact(Math.max(total, loaded))}`
  return `${range} of ${formatCountExact(Math.max(total, loaded))}`
}

export function tcpFlagLetters(flags: number | null): string {
  if (flags === null || flags === undefined) return ''
  const out: string[] = []
  if (flags & 32) out.push('U')
  if (flags & 16) out.push('A')
  if (flags & 8) out.push('P')
  if (flags & 4) out.push('R')
  if (flags & 2) out.push('S')
  if (flags & 1) out.push('F')
  return out.join('') || '·'
}

/** TCP flags as shown in the table: letters, or why there are none. */
export function flagsText(row: Pick<ForensicsRow, 'protocol' | 'tcp_flags' | 'flags_state'>): { text: string; title: string } {
  if (row.protocol !== 6) return { text: '—', title: 'Not TCP' }
  if (row.flags_state === 'absent') return { text: 'n/e', title: 'The exporter did not export TCP flags for this record' }
  const letters = tcpFlagLetters(row.tcp_flags)
  if (row.flags_state === 'legacy') {
    return { text: letters, title: 'Recorded before field tracking: a zero may mean "not exported"' }
  }
  return { text: letters, title: letters === '·' ? 'Exported and zero (no flags set)' : `Flags ${letters}` }
}

export function interfaceText(iface: ForensicsInterface | null, index: number | null): string {
  if (index === null || index === undefined) return 'n/e'
  if (index === 0) return '—'
  return iface?.display_name || iface?.if_name || iface?.if_descr || iface?.if_alias || `ifIndex ${index}`
}

/* ─── CSV of displayed rows ────────────────────────────────────────── */

const FORMULA = /^[=+\-@\t\r]/

/** Neutralise spreadsheet formulas (X-45) and quote as CSV. */
export function csvCell(value: unknown): string {
  if (value === null || value === undefined) return ''
  let s = String(value)
  if (typeof value === 'string' && FORMULA.test(s)) s = `'${s}`
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function csvValues(row: ForensicsRow, col: string): unknown {
  switch (col) {
    case 'timestamp_utc': return row.timestamp
    case 'received_at_utc': return row.received_at
    case 'exporter': return row.exporter
    case 'src': return row.src
    case 'src_port': return isIcmpRow(row) ? null : row.src_port
    case 'dst': return row.dst
    case 'dst_port': return isIcmpRow(row) ? null : row.dst_port
    case 'protocol': return row.protocol
    case 'protocol_name': return row.protocol_name
    case 'icmp_type': return isIcmpRow(row) ? row.icmp_type ?? (row.dst_port !== null ? (row.dst_port >> 8) & 0xff : null) : null
    case 'icmp_code': return isIcmpRow(row) ? row.icmp_code ?? (row.dst_port !== null ? row.dst_port & 0xff : null) : null
    case 'service': return row.service
    case 'application': return row.application
    case 'tcp_flags': return row.protocol === 6 && row.flags_state !== 'absent' ? tcpFlagLetters(row.tcp_flags) : ''
    case 'dscp': return row.dscp
    case 'input_if': return row.input_if
    case 'input_interface': return row.input_if ? interfaceText(row.input_interface, row.input_if) : ''
    case 'output_if': return row.output_if
    case 'output_interface': return row.output_if ? interfaceText(row.output_interface, row.output_if) : ''
    case 'bytes': return row.bytes
    case 'packets': return row.packets
    case 'duration_ms': return row.duration_ms
    case 'sampling_interval': return row.sampling_interval
    case 'flow_direction': return row.flow_direction
    case 'src_as': return row.src_as
    case 'dst_as': return row.dst_as
    case 'next_hop': return row.next_hop
    case 'nat_src': return row.nat?.src
    case 'nat_src_port': return row.nat?.src_port
    case 'nat_dst': return row.nat?.dst
    case 'nat_dst_port': return row.nat?.dst_port
    case 'fw_event': return row.fw_event
    default: return ''
  }
}

/** CSV of exactly the rows on screen, in the chosen columns (labelled as such by the caller). */
export function displayedRowsCsv(rows: ForensicsRow[], columns: string[]): string {
  const headers = COLUMNS.filter((c) => columns.includes(c.key)).flatMap((c) => c.csv)
  const lines = [headers.join(',')]
  for (const r of rows) lines.push(headers.map((h) => csvCell(csvValues(r, h))).join(','))
  return lines.join('\r\n') + '\r\n'
}

export function displayedRowsFilename(count: number, nowMs: number): string {
  const stamp = new Date(nowMs).toISOString().replace(/[-:]/g, '').slice(0, 15)
  return `netflow-forensics-displayed-${count}-rows-${stamp}Z.csv`
}

/* ─── Export choices ───────────────────────────────────────────────── */

export type ExportMode = 'stream' | 'job' | 'too_large' | 'forbidden' | 'unknown_total' | 'none'

/** How "export all matching rows" can be done for this result. */
export function exportMode(opts: {
  allowed: boolean
  total: number | null
  exact: boolean
  streamMax: number
  jobMax: number
}): ExportMode {
  if (!opts.allowed) return 'forbidden'
  if (opts.total === null) return 'unknown_total'
  if (opts.total === 0) return 'none'
  if (opts.exact && opts.total <= opts.streamMax) return 'stream'
  if (opts.total > opts.jobMax) return 'too_large'
  return 'job'
}

export function jobIsActive(job: Pick<ExportJob, 'status'>): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/* ─── Finding evidence (NF-09) ─────────────────────────────────────── */

const DETECTOR_LABELS: Record<string, string> = {
  syn_scan: 'SYN scan',
  port_scan: 'Port scan',
  horizontal_scan: 'Horizontal scan',
  vertical_scan: 'Vertical scan',
  rst_flood: 'TCP RST flood',
  icmp_flood: 'ICMP flood',
  sensitive_egress: 'Sensitive service egress',
  rfc1918_leak: 'Sensitive service egress',
  volumetric_outlier: 'Volumetric outlier',
  ti_match: 'Threat-intelligence match',
  threat_intel: 'Threat-intelligence match',
  beaconing: 'Beaconing',
  exfiltration: 'Possible exfiltration',
}

export function detectorLabel(kind: string | null | undefined): string {
  if (!kind) return 'Finding'
  return DETECTOR_LABELS[kind] ?? kind.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}

/** The host a finding is about: its `src` (or `talker`) filter, else the finding's own source. */
export function evidenceHost(filter: FlowFilter, findingSrc?: string | null): string | null {
  const one = (v?: string) => (v && !v.includes('/') ? v : null)
  return one(filter.src) ?? one(filter.talker) ?? (findingSrc ? ipDisplay(findingSrc) : null)
}

/**
 * "Broaden to host": every flow of the host in the same (frozen) window, without the detector's
 * protocol/port predicates; the exporter scope of the evidence is kept.
 */
export function broadenToHostLink(sp: URLSearchParams, host: string, exporter?: string): string {
  const next = resetSearchParams(sp)
  const filter: FlowFilter = { talker: host }
  if (exporter) filter.exporter = exporter
  return buildNetflowLink(`/netflow/forensics?${next.toString()}`, { filter, extra: { run: true } })
}

/* ─── Saved views ──────────────────────────────────────────────────── */

export interface SavedViewGroups {
  pinned: SavedView[]
  mine: SavedView[]
  shared: SavedView[]
  /** Other users' private views: only administrators receive them (for moderation). */
  othersPrivate: SavedView[]
}

function matchesSearch(v: SavedView, q: string): boolean {
  if (!q) return true
  const needle = q.toLowerCase()
  return (
    v.name.toLowerCase().includes(needle) ||
    (v.description ?? '').toLowerCase().includes(needle) ||
    (v.owner_username ?? '').toLowerCase().includes(needle)
  )
}

/**
 * Pinned first, then the caller's own, then views shared by others, then (administrators only)
 * other users' private views; each keeps the API order. Another user's private view is never
 * presented as pinned or shared.
 */
export function groupSavedViews(views: SavedView[], q = '', kind: SavedViewKind | 'all' = 'all'): SavedViewGroups {
  const out: SavedViewGroups = { pinned: [], mine: [], shared: [], othersPrivate: [] }
  for (const v of views) {
    if (kind !== 'all' && v.kind !== kind) continue
    if (!matchesSearch(v, q.trim())) continue
    if (!v.is_owner && v.visibility === 'private') out.othersPrivate.push(v)
    else if (v.pinned) out.pinned.push(v)
    else if (v.is_owner) out.mine.push(v)
    else out.shared.push(v)
  }
  return out
}

/** The stored query in v2 form, plus the page path a view reopens. */
export function savedViewQuery(
  v: Pick<SavedView, 'query' | 'kind'> & { legacy_time?: boolean },
): { query: SavedQueryV2; path: string | undefined; legacyTime: boolean } {
  const m = migrateSavedQuery(v.query, v.kind)
  const raw = v.query as { path?: unknown } | null
  const path = raw && typeof raw.path === 'string' && /^\/netflow(\/[A-Za-z0-9._~:%@-]+){0,3}$/.test(raw.path) ? raw.path : undefined
  // The API upgrades v1 blobs before sending them and reports the missing period as legacy_time.
  return { query: m.query, path, legacyTime: m.legacyTime || v.legacy_time === true }
}

/** Open link for a saved view: exactly its filter, window, sort, order and columns (+ its id). */
export function savedViewLink(v: Pick<SavedView, 'id' | 'query' | 'kind'>): string {
  const { query, path } = savedViewQuery(v)
  const link = savedQueryLink(query, path)
  return link + (link.includes('?') ? '&' : '?') + `view=${encodeURIComponent(v.id)}`
}

const RANGE_LABELS: Record<string, string> = {
  '1h': 'Last 1h', '24h': 'Last 24h', '7d': 'Last 7d', '1M': 'Last 1M', month: 'This month',
  last_month: 'Last month', quarter: 'This quarter',
}

export function savedWindowLabel(w: SavedQueryWindow, tz: string): string {
  if (w.mode === 'relative') return RANGE_LABELS[w.range] ?? w.range
  return `${formatDateTime(w.from, tz, { zone: 'none', seconds: false })} → ${formatDateTime(w.to, tz, { zone: 'offset', seconds: false })}`
}

export function sortLabel(q: Pick<SavedQueryV2, 'sort' | 'order'>): string | null {
  if (!q.sort) return null
  const label = SORT_LABELS[q.sort as ForensicsSort] ?? q.sort
  return `Sorted by ${label.toLowerCase()}, ${q.order === 'asc' ? 'ascending' : 'descending'}`
}

/**
 * Page settings a section view saves (`query.params`, FE-10): the ranking side (`by`) and the
 * address side (`side`) whenever the section has that control — explicitly, even at their
 * defaults, so a later default change cannot alter a saved view — plus name resolution and the
 * table's column sort.
 */
export function sectionViewParams(
  controls: readonly string[],
  s: { by: string; side: string; resolve: boolean; sortKey: string | null; sortDir: 'asc' | 'desc' },
): Record<string, string | null> {
  return {
    by: controls.includes('by') ? s.by : null,
    side: controls.includes('side') ? s.side : null,
    resolve: controls.includes('resolve') && s.resolve ? '1' : null,
    col: s.sortKey || null,
    dir: s.sortKey ? s.sortDir : null,
  }
}

/**
 * The saved page settings of an overview/section view in words ("Ranked by packets received ·
 * Destination side"), so the save dialog and Saved Views say what the view reopens. `metric` is
 * the section's ranked metric (its `sort`).
 */
export function pageSettingsSummary(params: Record<string, string> | null | undefined, metric?: string | null): string | null {
  if (!params) return null
  const parts: string[] = []
  const what = metric === 'packets' || metric === 'flows' ? metric : 'bytes'
  if (params.by === 'src') parts.push(`Ranked by ${what} sent`)
  else if (params.by === 'dst') parts.push(`Ranked by ${what} received`)
  else if (params.by === 'total') parts.push(`Ranked by all ${what} the host took part in`)
  if (params.side === 'src') parts.push('Source side')
  else if (params.side === 'dst') parts.push('Destination side')
  if (params.resolve === '1') parts.push('Host names resolved')
  if (params.col) parts.push(`Table sorted by ${params.col}, ${params.dir === 'asc' ? 'ascending' : 'descending'}`)
  return parts.length ? parts.join(' · ') : null
}

export const KIND_LABELS: Record<SavedViewKind, string> = {
  forensics: 'Forensics',
  overview: 'Overview',
  section: 'Breakdown',
}

/** The alias keys a URL may still carry (for tests and link hygiene). */
export const FILTER_ALIAS_KEYS: string[] = Object.values(FLOW_FILTER_ALIASES).flat() as string[]

export type { NfMeta }
