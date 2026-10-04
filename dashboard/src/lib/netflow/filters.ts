/**
 * The NetFlow filter contract (docs/netflow-review-contract.md §8), shared by every NetFlow page,
 * drill-down link and saved view.
 *
 * One typed `FlowFilter` with exactly the canonical URL/API keys replaces the per-page ad-hoc
 * parameter handling that lost filters between the overview, the detail panels and Forensics
 * (NF-08), sent `proto` from one page and `protocol` from another, and dropped numeric zero values
 * through truthiness tests (NF-12). Rules:
 *
 * - Numeric zero is a value everywhere (`dscp=0`, `port=0`); only absence is "no filter".
 * - Invalid values are never silently ignored: parsing reports them, validation explains them, and
 *   the API rejects them with a 400, so a filter chip can never sit on top of unfiltered totals.
 * - Enumerated names that are data-driven (applications, network classes, IP groups) are checked
 *   for shape here and for existence by the API, which owns the lists.
 */
import { normalizeIpText, parseIpOrCidr, isIpAddress } from './ip'
import {
  dscpLabel,
  dowName,
  formatBytes,
  formatCountExact,
  hourRangeLabel,
  ipDisplay,
  isValidTimeZone,
  protocolLabel,
} from './format'

export const FLOW_FILTER_KEYS = [
  'exporter',
  'iface',
  'iface_dir',
  'talker',
  'src',
  'dst',
  'not_talker',
  'not_src',
  'not_dst',
  'protocol',
  'port',
  'src_port',
  'dst_port',
  'dst_ports',
  'service_port',
  'app',
  'dscp',
  'netclass',
  'tcpflag',
  'hour',
  'dow',
  'tz',
  'group',
  'asn',
  'ip_version',
  'direction',
  'min_bytes',
  'max_bytes',
  'fw_event',
  'count',
] as const

export type FlowFilterKey = (typeof FLOW_FILTER_KEYS)[number]

export const IFACE_DIRECTIONS = ['in', 'out', 'any'] as const
export type IfaceDirection = (typeof IFACE_DIRECTIONS)[number]

export const TCP_FLAG_FILTERS = ['syn_only', 'ack_only', 'rst', 'fin', 'psh', 'urg', 'no_flags'] as const
export type TcpFlagFilter = (typeof TCP_FLAG_FILTERS)[number]

export const FLOW_DIRECTIONS = ['ingress', 'egress'] as const
export type FlowDirectionFilter = (typeof FLOW_DIRECTIONS)[number]

/**
 * `flows` = every record that carries traffic volume: all but NSEL connection-created and denied
 * events, which volume totals and cards leave out (contract §3.3). Drill-downs from volume cards
 * apply it so Forensics lists the records the card counted.
 */
export const FW_EVENT_FLOWS = 'flows'
export const FW_EVENTS = ['created', 'teardown', 'denied', 'alert', 'update', FW_EVENT_FLOWS] as const
export type FwEventFilter = (typeof FW_EVENTS)[number]

export const COUNTING_MODES = ['all', 'edge'] as const
export type CountingMode = (typeof COUNTING_MODES)[number]

export interface FlowFilter {
  /** Exporter address (IPv4 or IPv6). */
  exporter?: string
  /** ifIndex on either direction: `N`, or `N@exporter` to restrict it to one exporter. */
  iface?: string
  iface_dir?: IfaceDirection
  /** Source **or** destination (IP or CIDR). */
  talker?: string
  src?: string
  dst?: string
  not_talker?: string
  not_src?: string
  not_dst?: string
  protocol?: number
  /** Source or destination port. */
  port?: number
  src_port?: number
  dst_port?: number
  /** Destination port in this set (≤ 32 ports). */
  dst_ports?: number[]
  /** Classified service port: both directions of a service. */
  service_port?: number
  app?: string
  dscp?: number
  netclass?: string
  tcpflag?: TcpFlagFilter
  /** Hour of day 0..23, evaluated in `tz`. */
  hour?: number
  /** ISO weekday 1 (Monday) .. 7 (Sunday), evaluated in `tz`. */
  dow?: number
  tz?: string
  group?: string
  asn?: number
  ip_version?: 4 | 6
  direction?: FlowDirectionFilter
  min_bytes?: number
  max_bytes?: number
  fw_event?: FwEventFilter
  count?: CountingMode
}

export type FlowFilterErrors = Partial<Record<FlowFilterKey, string>>
export type FlowFilterDraft = Partial<Record<FlowFilterKey, string>>

export const FLOW_FILTER_LABELS: Record<FlowFilterKey, string> = {
  exporter: 'Exporter',
  iface: 'Interface',
  iface_dir: 'Interface direction',
  talker: 'Host',
  src: 'Source',
  dst: 'Destination',
  not_talker: 'Not host',
  not_src: 'Not source',
  not_dst: 'Not destination',
  protocol: 'Protocol',
  port: 'Port',
  src_port: 'Source port',
  dst_port: 'Destination port',
  dst_ports: 'Destination ports',
  service_port: 'Service port',
  app: 'Application',
  dscp: 'DSCP',
  netclass: 'Network class',
  tcpflag: 'TCP flags',
  hour: 'Hour',
  dow: 'Day',
  tz: 'Timezone',
  group: 'IP group',
  asn: 'AS number',
  ip_version: 'IP version',
  direction: 'Flow direction',
  min_bytes: 'Min bytes',
  max_bytes: 'Max bytes',
  fw_event: 'Firewall event',
  count: 'Counting',
}

/** Legacy read aliases: old bookmarks and saved views used these names. */
export const FLOW_FILTER_ALIASES: Partial<Record<FlowFilterKey, readonly string[]>> = {
  protocol: ['proto'],
  dscp: ['dscp_eq'],
}

export const TCP_FLAG_LABELS: Record<TcpFlagFilter, string> = {
  syn_only: 'SYN without ACK',
  ack_only: 'ACK without SYN',
  rst: 'RST set',
  fin: 'FIN set',
  psh: 'PSH set',
  urg: 'URG set',
  no_flags: 'No flags (exported as zero)',
}

export const FW_EVENT_LABELS: Record<FwEventFilter, string> = {
  created: 'Flow created',
  teardown: 'Flow torn down',
  denied: 'Denied',
  alert: 'Alert',
  update: 'Update',
  flows: 'Flow records only (no create/deny events)',
}

const UINT32_MAX = 4_294_967_295
const MAX_SAFE_BYTES = Number.MAX_SAFE_INTEGER
const NAME_MAX = 120

type Spec =
  | { kind: 'ip' }
  | { kind: 'ipOrCidr' }
  | { kind: 'int'; min: number; max: number; allowed?: readonly number[] }
  | { kind: 'enum'; values: readonly string[] }
  | { kind: 'ports' }
  | { kind: 'iface' }
  | { kind: 'name' }
  | { kind: 'tz' }

const SPECS: Record<FlowFilterKey, Spec> = {
  exporter: { kind: 'ip' },
  iface: { kind: 'iface' },
  iface_dir: { kind: 'enum', values: IFACE_DIRECTIONS },
  talker: { kind: 'ipOrCidr' },
  src: { kind: 'ipOrCidr' },
  dst: { kind: 'ipOrCidr' },
  not_talker: { kind: 'ipOrCidr' },
  not_src: { kind: 'ipOrCidr' },
  not_dst: { kind: 'ipOrCidr' },
  protocol: { kind: 'int', min: 0, max: 255 },
  port: { kind: 'int', min: 0, max: 65535 },
  src_port: { kind: 'int', min: 0, max: 65535 },
  dst_port: { kind: 'int', min: 0, max: 65535 },
  dst_ports: { kind: 'ports' },
  service_port: { kind: 'int', min: 0, max: 65535 },
  app: { kind: 'name' },
  dscp: { kind: 'int', min: 0, max: 63 },
  netclass: { kind: 'name' },
  tcpflag: { kind: 'enum', values: TCP_FLAG_FILTERS },
  hour: { kind: 'int', min: 0, max: 23 },
  dow: { kind: 'int', min: 1, max: 7 },
  tz: { kind: 'tz' },
  group: { kind: 'name' },
  asn: { kind: 'int', min: 0, max: UINT32_MAX },
  ip_version: { kind: 'int', min: 4, max: 6, allowed: [4, 6] },
  direction: { kind: 'enum', values: FLOW_DIRECTIONS },
  min_bytes: { kind: 'int', min: 0, max: MAX_SAFE_BYTES },
  max_bytes: { kind: 'int', min: 0, max: MAX_SAFE_BYTES },
  fw_event: { kind: 'enum', values: FW_EVENTS },
  count: { kind: 'enum', values: COUNTING_MODES },
}

export const DST_PORTS_MAX = 32

type ParseOutcome = { ok: true; value: unknown } | { ok: false; error: string }

function parseUint(text: string, max: number): number | null {
  if (!/^\d{1,16}$/.test(text)) return null
  const n = Number(text)
  return Number.isSafeInteger(n) && n <= max ? n : null
}

function parseValue(key: FlowFilterKey, raw: string): ParseOutcome {
  const text = raw.trim()
  const label = FLOW_FILTER_LABELS[key]
  const spec = SPECS[key]
  switch (spec.kind) {
    case 'ip':
      return isIpAddress(text)
        ? { ok: true, value: normalizeIpText(text) }
        : { ok: false, error: `${label} must be an IPv4 or IPv6 address` }
    case 'ipOrCidr':
      return parseIpOrCidr(text)
        ? { ok: true, value: normalizeIpText(text) }
        : { ok: false, error: `${label} must be an IP address or CIDR prefix, e.g. 10.0.0.5 or 10.0.0.0/8` }
    case 'int': {
      const n = parseUint(text, spec.max)
      const inRange = n !== null && n >= spec.min && (!spec.allowed || spec.allowed.includes(n))
      if (inRange) return { ok: true, value: n }
      if (spec.allowed) return { ok: false, error: `${label} must be ${spec.allowed.join(' or ')}` }
      return {
        ok: false,
        error: `${label} must be a whole number from ${formatCountExact(spec.min)} to ${formatCountExact(spec.max)}`,
      }
    }
    case 'enum':
      return spec.values.includes(text)
        ? { ok: true, value: text }
        : { ok: false, error: `${label} must be one of: ${spec.values.join(', ')}` }
    case 'ports': {
      const parts = text.split(',').map((p) => p.trim())
      const ports: number[] = []
      for (const p of parts) {
        const n = parseUint(p, 65535)
        if (n === null) {
          return { ok: false, error: `${label} must be comma-separated ports from 0 to 65535, e.g. 22,3389` }
        }
        if (!ports.includes(n)) ports.push(n)
      }
      if (ports.length > DST_PORTS_MAX) {
        return { ok: false, error: `${label} accepts at most ${DST_PORTS_MAX} ports` }
      }
      return { ok: true, value: ports }
    }
    case 'iface': {
      const m = /^(\d{1,10})(?:@(.+))?$/.exec(text)
      const ifindex = m ? parseUint(m[1], UINT32_MAX) : null
      if (!m || ifindex === null || (m[2] !== undefined && !isIpAddress(m[2].trim()))) {
        return {
          ok: false,
          error: `${label} must be an ifIndex from 0 to 4,294,967,295, optionally qualified as ifIndex@exporter`,
        }
      }
      return { ok: true, value: formatIfaceValue(ifindex, m[2]) }
    }
    case 'name':
      if (!text) return { ok: false, error: `${label} cannot be empty` }
      if (text.length > NAME_MAX) return { ok: false, error: `${label} must be ${NAME_MAX} characters or fewer` }
      // eslint-disable-next-line no-control-regex
      if (/[\u0000-\u001f\u007f]/.test(text)) return { ok: false, error: `${label} contains control characters` }
      return { ok: true, value: text }
    case 'tz':
      return isValidTimeZone(text)
        ? { ok: true, value: text }
        : { ok: false, error: `${label} must be an IANA zone name such as Asia/Riyadh or UTC` }
  }
}

function hasValue(v: unknown): boolean {
  if (v === undefined || v === null || v === '') return false
  if (typeof v === 'number') return Number.isFinite(v)
  if (Array.isArray(v)) return v.length > 0
  return true
}

/** URL/API text for one filter value. */
export function encodeFlowFilterValue(key: FlowFilterKey, value: unknown): string {
  if (key === 'dst_ports' && Array.isArray(value)) return value.join(',')
  return String(value)
}

/* ─── Interface references ─────────────────────────────────────────── */

export interface IfaceRef {
  ifindex: number
  /** Present when the filter is qualified to one exporter. */
  exporter?: string
}

/** `iface` value for an ifIndex, qualified with its exporter when known ("3@192.0.2.1"). */
export function formatIfaceValue(ifindex: number, exporter?: string | null): string {
  const ex = exporter?.trim()
  return ex ? `${ifindex}@${normalizeIpText(ex)}` : String(ifindex)
}

export function parseIfaceValue(value: string | null | undefined): IfaceRef | null {
  if (!value) return null
  const parsed = parseValue('iface', value)
  if (!parsed.ok) return null
  const [idx, exporter] = String(parsed.value).split('@')
  return exporter ? { ifindex: Number(idx), exporter } : { ifindex: Number(idx) }
}

/* ─── Parse / write ────────────────────────────────────────────────── */

export interface ParsedFlowFilter {
  /** Valid values only. */
  filter: FlowFilter
  /** One message per invalid or contradictory key. */
  errors: FlowFilterErrors
  /** The raw text of every invalid value, for "remove invalid filter" UIs. */
  invalid: FlowFilterDraft
}

function readFilter(get: (key: string) => string | null | undefined): ParsedFlowFilter {
  const filter: Record<string, unknown> = {}
  const errors: FlowFilterErrors = {}
  const invalid: FlowFilterDraft = {}
  for (const key of FLOW_FILTER_KEYS) {
    let raw = get(key)
    if (raw == null) {
      for (const alias of FLOW_FILTER_ALIASES[key] ?? []) {
        raw = get(alias)
        if (raw != null) break
      }
    }
    if (raw == null || raw.trim() === '') continue
    const out = parseValue(key, raw)
    if (out.ok) filter[key] = out.value
    else {
      errors[key] = out.error
      invalid[key] = raw
    }
  }
  const f = filter as FlowFilter
  return { filter: f, errors: { ...crossFieldErrors(f), ...errors }, invalid }
}

/**
 * Every valid filter in a query string (canonical keys, then the legacy aliases `proto` and
 * `dscp_eq`). Use `parseFlowFilterDetailed` to also learn which values were rejected: a page must
 * say so rather than run the query unfiltered.
 */
export function parseFlowFilter(sp: URLSearchParams): FlowFilter {
  return readFilter((k) => sp.get(k)).filter
}

export function parseFlowFilterDetailed(sp: URLSearchParams): ParsedFlowFilter {
  return readFilter((k) => sp.get(k))
}

/** Parse form inputs with the same rules as the URL (used by draft/applied search forms). */
export function flowFilterFromDraft(draft: FlowFilterDraft): ParsedFlowFilter {
  return readFilter((k) => draft[k as FlowFilterKey])
}

/** Form text for every key ('' when absent). */
export function flowFilterToDraft(f: FlowFilter): Record<FlowFilterKey, string> {
  const out = {} as Record<FlowFilterKey, string>
  for (const key of FLOW_FILTER_KEYS) {
    const v = (f as Record<string, unknown>)[key]
    out[key] = hasValue(v) ? encodeFlowFilterValue(key, v) : ''
  }
  return out
}

/**
 * A copy of `sp` whose filter keys are exactly `f` in canonical form: every canonical key and
 * legacy alias not in `f` is removed, other parameters (range, sort, …) are kept.
 */
export function writeFlowFilter(sp: URLSearchParams, f: FlowFilter): URLSearchParams {
  const next = new URLSearchParams(sp)
  for (const key of FLOW_FILTER_KEYS) {
    next.delete(key)
    for (const alias of FLOW_FILTER_ALIASES[key] ?? []) next.delete(alias)
  }
  for (const key of FLOW_FILTER_KEYS) {
    const v = (f as Record<string, unknown>)[key]
    if (hasValue(v)) next.set(key, encodeFlowFilterValue(key, v))
  }
  return next
}

/**
 * `writeFlowFilter`, but invalid values that came with the link (`invalid`) stay in the URL unless
 * `next` sets that key or it is listed in `drop`: changing one filter must not silently discard a
 * value the page is reporting as "not applied".
 */
export function writeFlowFilterKeepingInvalid(
  sp: URLSearchParams,
  next: FlowFilter,
  invalid: FlowFilterDraft,
  drop: readonly FlowFilterKey[] = [],
): URLSearchParams {
  const out = writeFlowFilter(sp, next)
  for (const key of FLOW_FILTER_KEYS) {
    const raw = invalid[key]
    if (raw === undefined || drop.includes(key) || hasValue((next as Record<string, unknown>)[key])) continue
    out.set(key, raw)
  }
  return out
}

/** Request parameters for the API: canonical keys, present values only. */
export function flowFilterToApiParams(f: FlowFilter): Record<string, string> {
  const out: Record<string, string> = {}
  for (const key of FLOW_FILTER_KEYS) {
    const v = (f as Record<string, unknown>)[key]
    if (hasValue(v)) out[key] = encodeFlowFilterValue(key, v)
  }
  return out
}

export function isFlowFilterEmpty(f: FlowFilter): boolean {
  return FLOW_FILTER_KEYS.every((k) => !hasValue((f as Record<string, unknown>)[k]))
}

/** Number of narrowing filters (`tz` and `count=all` do not narrow anything). */
export function countFlowFilters(f: FlowFilter): number {
  return FLOW_FILTER_KEYS.filter((k) => {
    if (k === 'tz') return false
    if (k === 'count' && f.count === 'all') return false
    return hasValue((f as Record<string, unknown>)[k])
  }).length
}

export function flowFiltersEqual(a: FlowFilter, b: FlowFilter): boolean {
  return new URLSearchParams(flowFilterToApiParams(a)).toString() === new URLSearchParams(flowFilterToApiParams(b)).toString()
}

/** `f` with `patch` applied; an `undefined`/`null` patch value removes that key. */
export function mergeFlowFilter(f: FlowFilter, patch: Partial<Record<FlowFilterKey, unknown>>): FlowFilter {
  const next: Record<string, unknown> = { ...f }
  for (const [k, v] of Object.entries(patch)) {
    if (!hasValue(v)) delete next[k]
    else next[k] = v
  }
  if (!hasValue(next.iface)) delete next.iface_dir
  return next as FlowFilter
}

/** `f` without `keys`; removing the interface also removes its direction. */
export function removeFlowFilterKeys(f: FlowFilter, keys: readonly FlowFilterKey[]): FlowFilter {
  const next: Record<string, unknown> = { ...f }
  for (const k of keys) delete next[k]
  if (keys.includes('iface')) delete next.iface_dir
  return next as FlowFilter
}

/**
 * Filter keys the page requested that the response reports as not applied (`meta.filters` lists
 * what was applied). `tz` belongs to the window, and `count=all` is the default, so neither counts.
 */
export function unappliedFilterKeys(requested: FlowFilter, applied: Record<string, unknown>): FlowFilterKey[] {
  return (Object.keys(flowFilterToApiParams(requested)) as FlowFilterKey[]).filter((k) => {
    if (k === 'tz' || (k === 'count' && requested.count === 'all')) return false
    return applied[k] === undefined || applied[k] === null
  })
}

/* ─── Validation ───────────────────────────────────────────────────── */

function crossFieldErrors(f: FlowFilter): FlowFilterErrors {
  const errors: FlowFilterErrors = {}
  if (hasValue(f.iface_dir) && !hasValue(f.iface)) {
    errors.iface_dir = 'Choose an interface for the direction to apply to'
  }
  if (hasValue(f.tcpflag) && hasValue(f.protocol) && f.protocol !== 6) {
    errors.tcpflag = 'TCP flag filters only match TCP (protocol 6)'
  }
  if (hasValue(f.min_bytes) && hasValue(f.max_bytes) && (f.max_bytes as number) < (f.min_bytes as number)) {
    errors.max_bytes = 'Max bytes must be at least Min bytes'
  }
  return errors
}

/**
 * Field messages for a filter built in code or restored from storage: each value is re-checked
 * against the URL rules (ranges, formats, enums), then the cross-field rules apply. Empty when valid.
 */
export function validateFlowFilter(f: FlowFilter): FlowFilterErrors {
  const errors: FlowFilterErrors = {}
  for (const key of FLOW_FILTER_KEYS) {
    const v = (f as Record<string, unknown>)[key]
    if (!hasValue(v)) continue
    if (typeof v === 'number' && !Number.isInteger(v)) {
      errors[key] = `${FLOW_FILTER_LABELS[key]} must be a whole number`
      continue
    }
    const out = parseValue(key, encodeFlowFilterValue(key, v))
    if (!out.ok) errors[key] = out.error
  }
  return { ...crossFieldErrors(f), ...errors }
}

/* ─── Description (chips) ──────────────────────────────────────────── */

export interface FlowFilterChip {
  /** The key the chip is about. */
  key: FlowFilterKey
  /** Keys removed together when the chip is dismissed. */
  keys: FlowFilterKey[]
  label: string
  value: string
  /** Semantics the value alone does not state ("source or destination"). */
  hint?: string
  /** An exclusion filter. */
  negated?: boolean
}

export interface DescribeOptions {
  /** Zone that hour/day filters are evaluated in when the filter carries no `tz` (the page window's). */
  tz?: string
}

/** One chip per applied filter, in canonical order, stating exactly what is matched. */
export function describeFlowFilter(f: FlowFilter, opts: DescribeOptions = {}): FlowFilterChip[] {
  const chips: FlowFilterChip[] = []
  const zone = f.tz ?? opts.tz ?? 'UTC'
  const add = (key: FlowFilterKey, value: string, extra: Partial<FlowFilterChip> = {}) =>
    chips.push({ key, keys: [key], label: FLOW_FILTER_LABELS[key], value, ...extra })

  if (hasValue(f.exporter)) add('exporter', ipDisplay(f.exporter))
  if (hasValue(f.iface)) {
    const ref = parseIfaceValue(f.iface)
    const where = ref?.exporter ? ` on ${ipDisplay(ref.exporter)}` : ref ? ' on any exporter' : ''
    const dir = f.iface_dir === 'in' ? 'inbound (input) only' : f.iface_dir === 'out' ? 'outbound (output) only' : 'input or output'
    add('iface', ref ? `ifIndex ${ref.ifindex}${where}` : String(f.iface), { keys: ['iface', 'iface_dir'], hint: dir })
  } else if (hasValue(f.iface_dir)) {
    add('iface_dir', String(f.iface_dir))
  }
  if (hasValue(f.talker)) add('talker', ipDisplay(f.talker), { hint: 'source or destination' })
  if (hasValue(f.src)) add('src', ipDisplay(f.src))
  if (hasValue(f.dst)) add('dst', ipDisplay(f.dst))
  if (hasValue(f.not_talker)) add('not_talker', ipDisplay(f.not_talker), { negated: true, hint: 'excluded as source and destination' })
  if (hasValue(f.not_src)) add('not_src', ipDisplay(f.not_src), { negated: true })
  if (hasValue(f.not_dst)) add('not_dst', ipDisplay(f.not_dst), { negated: true })
  if (hasValue(f.protocol)) add('protocol', protocolLabel(f.protocol))
  if (hasValue(f.port)) add('port', String(f.port), { hint: 'source or destination port' })
  if (hasValue(f.src_port)) add('src_port', String(f.src_port))
  if (hasValue(f.dst_port)) add('dst_port', String(f.dst_port))
  if (hasValue(f.dst_ports)) add('dst_ports', (f.dst_ports ?? []).join(', '), { hint: 'destination port in this set' })
  if (hasValue(f.service_port)) add('service_port', String(f.service_port), { hint: 'both directions of the service' })
  if (hasValue(f.app)) add('app', String(f.app))
  if (hasValue(f.dscp)) add('dscp', dscpLabel(f.dscp))
  if (hasValue(f.netclass)) add('netclass', String(f.netclass), { hint: 'source or destination in this class' })
  if (hasValue(f.tcpflag)) {
    add('tcpflag', TCP_FLAG_LABELS[f.tcpflag as TcpFlagFilter] ?? String(f.tcpflag), {
      hint: 'TCP records whose flags were exported',
    })
  }
  if (hasValue(f.hour)) add('hour', `${hourRangeLabel(f.hour)} (${zone})`)
  if (hasValue(f.dow)) add('dow', `${dowName(f.dow)} (${zone})`)
  if (hasValue(f.group)) add('group', String(f.group), { hint: 'source or destination in this group' })
  if (hasValue(f.asn)) add('asn', `AS${f.asn}`, { hint: 'source or destination AS' })
  if (hasValue(f.ip_version)) add('ip_version', `IPv${f.ip_version}`)
  if (hasValue(f.direction)) {
    add('direction', f.direction === 'ingress' ? 'Ingress-monitored' : 'Egress-monitored', {
      hint: 'as reported by the exporter (IE 61)',
    })
  }
  if (hasValue(f.min_bytes)) add('min_bytes', `≥ ${formatBytes(f.min_bytes)}`, { hint: `${formatCountExact(f.min_bytes)} bytes per record` })
  if (hasValue(f.max_bytes)) add('max_bytes', `≤ ${formatBytes(f.max_bytes)}`, { hint: `${formatCountExact(f.max_bytes)} bytes per record` })
  if (hasValue(f.fw_event)) add('fw_event', FW_EVENT_LABELS[f.fw_event as FwEventFilter] ?? String(f.fw_event))
  if (f.count === 'edge') {
    add('count', 'Edge exporters only', { hint: 'each flow counted once at its edge collection point' })
  }
  return chips
}

/* ─── Saved queries (v2) ───────────────────────────────────────────── */

export const SAVED_VIEW_KINDS = ['forensics', 'overview', 'section'] as const
export type SavedViewKind = (typeof SAVED_VIEW_KINDS)[number]

/** Rolling presets and calendar periods a saved relative window may name. */
export const SAVED_RELATIVE_RANGES = ['1h', '24h', '7d', '1M', 'month', 'last_month', 'quarter'] as const
export type SavedRelativeRange = (typeof SAVED_RELATIVE_RANGES)[number]

export type SavedQueryWindow =
  | { mode: 'relative'; range: SavedRelativeRange }
  | { mode: 'fixed'; from: string; to: string }

export interface SavedQueryV2 {
  v: 2
  kind: SavedViewKind
  filter: FlowFilter
  window: SavedQueryWindow
  sort?: string
  order?: 'asc' | 'desc'
  columns?: string[]
  /**
   * Page settings that change what an overview/section page ranks or shows (`by` sent/received,
   * `side` source/destination, `resolve`, table `col`/`dir`), so a "top receivers" view never
   * reopens as "top senders" (FE-10). Validated against SAVED_PAGE_PARAMS here and by the API.
   */
  params?: Record<string, string>
}

export interface SavedQueryIssue {
  key: string
  value: string
  reason: string
}

export interface SavedQueryMigration {
  query: SavedQueryV2
  /** The stored query had no usable time window (v1 never saved one); `window` is the 1h default. */
  legacyTime: boolean
  /** Stored conditions that could not be restored, so the UI can say which. */
  dropped: SavedQueryIssue[]
}

const DEFAULT_SAVED_WINDOW: SavedQueryWindow = { mode: 'relative', range: '1h' }
const SORT_RE = /^[a-z][a-z0-9_]{0,40}$/

/** Page settings a saved view may store (`query.params`): key -> accepted values (the API's PAGE_PARAMS). */
export const SAVED_PAGE_PARAMS: Readonly<Record<string, readonly string[] | RegExp>> = {
  by: ['src', 'dst', 'total'],
  side: ['src', 'dst'],
  resolve: ['1'],
  col: SORT_RE,
  dir: ['asc', 'desc'],
}

/** The valid page settings of `raw` (unknown keys and values left out); undefined when none. */
export function savedPageParams(raw: unknown): Record<string, string> | undefined {
  if (!isRecord(raw)) return undefined
  const out: Record<string, string> = {}
  for (const key of Object.keys(SAVED_PAGE_PARAMS).sort()) {
    const v = raw[key]
    const text = typeof v === 'string' ? v : typeof v === 'number' && Number.isFinite(v) ? String(v) : null
    if (text === null || text === '') continue
    const rule = SAVED_PAGE_PARAMS[key]
    if (rule instanceof RegExp ? rule.test(text) : rule.includes(text)) out[key] = text
  }
  return Object.keys(out).length ? out : undefined
}
// Fields of a v1 (flat) forensics blob that are not filter conditions.
const V1_META_KEYS = new Set(['v', 'kind', 'sort', 'order', 'columns', 'time', 'window', 'filters', 'filter', 'name', 'pinned'])

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function jsonToText(v: unknown): string | null {
  if (typeof v === 'string') return v
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  if (Array.isArray(v) && v.every((x) => typeof x === 'number' || typeof x === 'string')) return v.join(',')
  return null
}

function restoreFilter(source: Record<string, unknown>, skip: Set<string>, dropped: SavedQueryIssue[]): FlowFilter {
  const draft: Record<string, string> = {}
  const known = new Set<string>(FLOW_FILTER_KEYS)
  const aliasOf = new Map<string, FlowFilterKey>()
  for (const key of FLOW_FILTER_KEYS) for (const a of FLOW_FILTER_ALIASES[key] ?? []) aliasOf.set(a, key)
  for (const [k, v] of Object.entries(source)) {
    if (skip.has(k) || v === null || v === undefined || v === '') continue
    const key = known.has(k) ? k : aliasOf.get(k)
    const text = jsonToText(v)
    if (!key) {
      dropped.push({ key: k, value: text ?? JSON.stringify(v), reason: 'not a NetFlow filter' })
      continue
    }
    if (text === null) {
      dropped.push({ key: k, value: JSON.stringify(v), reason: 'not a valid value' })
      continue
    }
    // A canonical key wins over its legacy alias.
    if (key !== k && draft[key] !== undefined) continue
    draft[key] = text
  }
  const parsed = flowFilterFromDraft(draft)
  for (const [k, raw] of Object.entries(parsed.invalid)) {
    dropped.push({ key: k, value: raw ?? '', reason: parsed.errors[k as FlowFilterKey] ?? 'invalid' })
  }
  return parsed.filter
}

function restoreWindow(raw: unknown): SavedQueryWindow | null {
  if (!isRecord(raw)) return null
  if (raw.mode === 'relative' && SAVED_RELATIVE_RANGES.includes(raw.range as SavedRelativeRange)) {
    return { mode: 'relative', range: raw.range as SavedRelativeRange }
  }
  if (raw.mode === 'fixed' && typeof raw.from === 'string' && typeof raw.to === 'string') {
    const a = Date.parse(raw.from)
    const b = Date.parse(raw.to)
    if (Number.isFinite(a) && Number.isFinite(b) && a < b) {
      return { mode: 'fixed', from: new Date(a).toISOString(), to: new Date(b).toISOString() }
    }
  }
  return null
}

/**
 * Read a stored saved-view query of any version into the v2 shape. v1 blobs (the old flat
 * forensics form `{src, dst, proto, src_port, …, sort, order}` with string values) are upgraded:
 * `proto` becomes `protocol`, numeric strings become numbers (`"0"` stays a DSCP 0 filter), empty
 * strings are absent, and the missing time window is reported through `legacyTime`.
 */
export function migrateSavedQuery(raw: unknown, fallbackKind: SavedViewKind = 'forensics'): SavedQueryMigration {
  const dropped: SavedQueryIssue[] = []
  const r = isRecord(raw) ? raw : {}
  const kind = SAVED_VIEW_KINDS.includes(r.kind as SavedViewKind) ? (r.kind as SavedViewKind) : fallbackKind
  const filterSource = r.v === 2 ? r.filter : isRecord(r.filters) ? r.filters : r
  const filter = restoreFilter(isRecord(filterSource) ? filterSource : {}, r.v === 2 ? new Set() : V1_META_KEYS, dropped)
  const win = restoreWindow(r.v === 2 ? r.window : r.time ?? r.window)
  const query: SavedQueryV2 = { v: 2, kind, filter, window: win ?? DEFAULT_SAVED_WINDOW }
  if (typeof r.sort === 'string' && r.sort !== '') {
    if (SORT_RE.test(r.sort)) query.sort = r.sort
    else dropped.push({ key: 'sort', value: r.sort, reason: 'not a sortable column' })
  }
  if (r.order === 'asc' || r.order === 'desc') query.order = r.order
  if (Array.isArray(r.columns)) {
    const cols = r.columns.filter((c): c is string => typeof c === 'string' && SORT_RE.test(c))
    if (cols.length) query.columns = cols
  }
  const params = r.v === 2 ? savedPageParams(r.params) : undefined
  if (params) query.params = params
  return { query, legacyTime: win === null, dropped }
}

export interface BuildSavedQueryInput {
  kind: SavedViewKind
  filter: FlowFilter
  window: SavedQueryWindow
  sort?: string | null
  order?: 'asc' | 'desc' | null
  columns?: string[] | null
  /** Page settings (`by`, `side`, …); invalid or empty values are left out. */
  params?: Record<string, string | null | undefined> | null
}

/** A v2 saved query from the applied page state (never from un-submitted drafts). */
export function buildSavedQuery(input: BuildSavedQueryInput): SavedQueryV2 {
  const query: SavedQueryV2 = {
    v: 2,
    kind: input.kind,
    filter: { ...input.filter },
    window: { ...input.window },
  }
  if (input.sort) query.sort = input.sort
  if (input.order) query.order = input.order
  if (input.columns?.length) query.columns = [...input.columns]
  const params = savedPageParams(input.params ?? undefined)
  if (params) query.params = params
  return query
}

/* ─── Saved-view dialog fields ─────────────────────────────────────── */

export const SAVED_VIEW_NAME_MAX = 120
export const SAVED_VIEW_DESCRIPTION_MAX = 500

export interface SaveViewFields {
  name: string
  description?: string
}

/** Messages for the save dialog; whitespace-only names are rejected (NF-34). */
export function validateSaveViewFields(d: SaveViewFields): { name?: string; description?: string } {
  const errors: { name?: string; description?: string } = {}
  const name = d.name.trim()
  if (!name) errors.name = 'Enter a name for this view'
  else if (name.length > SAVED_VIEW_NAME_MAX) errors.name = `Use ${SAVED_VIEW_NAME_MAX} characters or fewer`
  // eslint-disable-next-line no-control-regex
  else if (/[\u0000-\u001f\u007f]/.test(name)) errors.name = 'The name cannot contain control characters'
  if ((d.description ?? '').trim().length > SAVED_VIEW_DESCRIPTION_MAX) {
    errors.description = `Use ${SAVED_VIEW_DESCRIPTION_MAX} characters or fewer`
  }
  return errors
}
