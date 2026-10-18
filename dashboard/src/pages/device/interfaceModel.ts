/* Pure interface-table rules for the device Interfaces tab (no React, no
 * imports, so scripts/test-device-interfaces.mjs can load it directly).
 *
 * Status follows server/app/services/device_insights.py interface_breakdown:
 * only an *unexpected* down port is actionable — administratively up,
 * operationally down, monitored, and seen up at least once (the baseline the
 * Interface-down alert uses). Spare access ports and admin-down ports are not
 * failures. */

export type InterfaceRecord = {
  id: number
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_type: number | null
  if_speed: number | null
  configured_speed_bps: number | null
  mac_address: string | null
  admin_status: string | null
  oper_status: string | null
  monitored: boolean
  first_seen?: string | null
  last_seen: string | null
  last_up_at?: string | null
  ever_up?: boolean | null
}

export type ErrorTotals = { in_errors: number; out_errors: number; in_discards: number; out_discards: number }

/* ── Status ─────────────────────────────────────────────────────────────── */

export type InterfaceStatusKind = 'up' | 'down' | 'admin_down' | 'unused' | 'not_present' | 'unmonitored_down' | 'other'

export type InterfaceStatus = {
  kind: InterfaceStatusKind
  label: string
  /** 'crit' only for the actionable set; everything else stays neutral. */
  tone: 'ok' | 'crit' | 'neutral'
  explanation: string
}

const UP = new Set(['up', '1'])
const DOWN = new Set(['down', '2', 'lowerlayerdown', '7'])
const ABSENT = new Set(['notpresent', '6'])
const ADMIN_DOWN = new Set(['down', '2'])

export function normStatus(value: unknown): string {
  return String(value ?? '').replace(/[\s_-]/g, '').toLowerCase()
}

function titleCase(value: string): string {
  const s = value.replace(/([a-z])([A-Z])/g, '$1 $2').trim()
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Unknown'
}

export function classifyInterface(i: Pick<InterfaceRecord, 'oper_status' | 'admin_status' | 'monitored' | 'ever_up'>): InterfaceStatus {
  const oper = normStatus(i.oper_status)
  const admin = normStatus(i.admin_status)
  if (UP.has(oper)) return { kind: 'up', label: 'Up', tone: 'ok', explanation: 'Operationally up.' }
  if (ADMIN_DOWN.has(admin)) return { kind: 'admin_down', label: 'Admin down', tone: 'neutral', explanation: 'Shut down by configuration; not a failure.' }
  if (ABSENT.has(oper)) return { kind: 'not_present', label: 'Not present', tone: 'neutral', explanation: 'The hardware for this port (module, transceiver) is missing.' }
  if (DOWN.has(oper)) {
    if (!i.ever_up) return { kind: 'unused', label: 'Unused', tone: 'neutral', explanation: 'Down and never seen up: a spare port, not an outage.' }
    if (i.monitored === false) return { kind: 'unmonitored_down', label: 'Down', tone: 'neutral', explanation: 'Down, but the port is not monitored, so it raises no alert.' }
    return { kind: 'down', label: 'Down', tone: 'crit', explanation: 'Admin up, operationally down, monitored and previously up: an unexpected outage.' }
  }
  return { kind: 'other', label: titleCase(String(i.oper_status ?? '')), tone: 'neutral', explanation: 'Reported operational state (dormant, testing or unknown).' }
}

/** Actionable first, spare and absent ports last. */
export const STATUS_ORDER: Record<InterfaceStatusKind, number> = {
  down: 0, other: 1, unmonitored_down: 2, up: 3, admin_down: 4, unused: 5, not_present: 6,
}

/* ── Interface type groups (IANAifType) ─────────────────────────────────── */

export type InterfaceTypeGroup = 'ethernet' | 'lag' | 'vlan' | 'tunnel' | 'wireless' | 'loopback' | 'virtual' | 'other'

export const TYPE_GROUPS: Array<{ id: InterfaceTypeGroup; label: string }> = [
  { id: 'ethernet', label: 'Physical' },
  { id: 'lag', label: 'Aggregate / LAG' },
  { id: 'vlan', label: 'VLAN' },
  { id: 'tunnel', label: 'Tunnel' },
  { id: 'wireless', label: 'Wireless / SSID' },
  { id: 'loopback', label: 'Loopback' },
  { id: 'virtual', label: 'Virtual' },
  { id: 'other', label: 'Other' },
]

export function interfaceTypeGroup(ifType: number | null | undefined): InterfaceTypeGroup {
  switch (Number(ifType)) {
    case 6: case 62: case 117: return 'ethernet'
    case 135: case 136: return 'vlan'
    case 131: return 'tunnel'
    case 161: return 'lag'
    case 71: return 'wireless'
    case 24: return 'loopback'
    case 53: return 'virtual'
    default: return 'other'
  }
}

export function typeGroupLabel(group: InterfaceTypeGroup): string {
  return TYPE_GROUPS.find((g) => g.id === group)?.label ?? 'Other'
}

/* ── Names ──────────────────────────────────────────────────────────────── */

export function interfaceName(i: Pick<InterfaceRecord, 'if_name' | 'if_descr' | 'if_index'>): string {
  return (i.if_name || '').trim() || (i.if_descr || '').trim() || `ifIndex ${i.if_index}`
}

/** The operator's description (ifAlias). Never repeats the name: an empty alias
 *  or one equal to ifName / ifDescr is no description. */
export function interfaceDescription(i: Pick<InterfaceRecord, 'if_alias' | 'if_name' | 'if_descr'>): string | null {
  const alias = (i.if_alias || '').trim()
  if (!alias) return null
  const same = (v: string | null) => !!v && v.trim().toLowerCase() === alias.toLowerCase()
  return same(i.if_name) || same(i.if_descr) ? null : alias
}

/* ── Speed and rates ────────────────────────────────────────────────────── */

export function effectiveSpeed(i: Pick<InterfaceRecord, 'configured_speed_bps' | 'if_speed'>): number | null {
  const manual = Number(i.configured_speed_bps)
  if (manual > 0 && Number.isFinite(manual)) return manual
  const snmp = Number(i.if_speed)
  return snmp > 0 && Number.isFinite(snmp) ? snmp : null
}

/** Utilisation of the busier direction, as link utilisation and the summary report it. */
export function utilisationPct(inBps: number | null, outBps: number | null, speed: number | null): number | null {
  if (!speed || inBps == null || outBps == null) return null
  return Math.min(100, (Math.max(inBps, outBps) / speed) * 100)
}

/** Sparkline scale: from zero to the link speed when known (never shrunk below
 *  the data, so a burst is not clipped), otherwise to the series maximum. */
export function sparkDomain(values: Array<number | null>, speed: number | null): [number, number] {
  let max = 0
  for (const v of values) if (v != null && Number.isFinite(v) && v > max) max = v
  if (speed && speed > 0) return [0, Math.max(speed, max)]
  return [0, max > 0 ? max : 1]
}

export function errorSum(t: ErrorTotals | null | undefined): number | null {
  return t ? t.in_errors + t.out_errors : null
}

export function discardSum(t: ErrorTotals | null | undefined): number | null {
  return t ? t.in_discards + t.out_discards : null
}

/* ── Saved filters (URL ?ifilter=) ──────────────────────────────────────── */

export type SavedFilter = 'all' | 'uplinks' | 'errors' | 'unexpected_down'

export const SAVED_FILTERS: Array<{ id: SavedFilter; label: string; explanation: string }> = [
  { id: 'all', label: 'All', explanation: 'Every discovered interface.' },
  { id: 'uplinks', label: 'Uplinks / WAN', explanation: 'Name or description says WAN, uplink, ISP, DIA or internet, or the port is up at the fastest physical speed on the device (when ports run at different speeds).' },
  { id: 'errors', label: 'Errors', explanation: 'Errors or discards counted in the selected window.' },
  { id: 'unexpected_down', label: 'Unexpected down', explanation: 'Admin up, operationally down, monitored and previously seen up.' },
]

export function parseSavedFilter(value: string | null | undefined): SavedFilter {
  return SAVED_FILTERS.some((f) => f.id === value) ? (value as SavedFilter) : 'all'
}

// Same vocabulary as the summary endpoint's busiest-interface WAN flag.
const WAN_NAME = /^(wan\d*|internet\d*|isp\d*)$|\b(wan|isp|dia|internet|uplink)\b/i

export function looksLikeWan(i: Pick<InterfaceRecord, 'if_name' | 'if_descr' | 'if_alias'>): boolean {
  return [i.if_name, i.if_descr, i.if_alias].some((v) => !!v && WAN_NAME.test(v.trim()))
}

/** The fastest physical (Ethernet or LAG) speed among up ports, or null when
 *  every up physical port runs at the same speed (speed then says nothing
 *  about which ports are uplinks — a 48-port 1G access switch). */
export function uplinkSpeed(rows: Array<Pick<InterfaceRecord, 'if_type' | 'oper_status' | 'admin_status' | 'monitored' | 'ever_up' | 'configured_speed_bps' | 'if_speed'>>): number | null {
  const speeds: number[] = []
  for (const r of rows) {
    const group = interfaceTypeGroup(r.if_type)
    if (group !== 'ethernet' && group !== 'lag') continue
    if (classifyInterface(r).kind !== 'up') continue
    const s = effectiveSpeed(r)
    if (s) speeds.push(s)
  }
  if (!speeds.length) return null
  const max = Math.max(...speeds)
  return Math.min(...speeds) < max ? max : null
}

export type FilterContext = { uplinkSpeed: number | null; errorTotals: Record<string, ErrorTotals> | null }

export function matchesSavedFilter(row: InterfaceRecord, filter: SavedFilter, ctx: FilterContext): boolean {
  switch (filter) {
    case 'all':
      return true
    case 'uplinks': {
      if (looksLikeWan(row)) return true
      const s = effectiveSpeed(row)
      return ctx.uplinkSpeed != null && s === ctx.uplinkSpeed && classifyInterface(row).kind === 'up'
    }
    case 'errors': {
      const t = ctx.errorTotals?.[String(row.if_index)]
      return !!t && ((errorSum(t) ?? 0) > 0 || (discardSum(t) ?? 0) > 0)
    }
    case 'unexpected_down':
      return classifyInterface(row).kind === 'down'
  }
}

export function matchesSearch(row: Pick<InterfaceRecord, 'if_name' | 'if_descr' | 'if_alias' | 'if_index'>, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return [row.if_name, row.if_descr, row.if_alias, String(row.if_index)].some((v) => !!v && v.toLowerCase().includes(q))
}

/** Sort comparator that keeps unknown values last in either direction. */
export function compareNullable(a: number | null, b: number | null, dir: 1 | -1): number {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  return dir * (a - b)
}

/** Where the retired /devices/:id/interfaces page sends a link: the device
 *  shell's Interfaces tab, keeping every other search parameter (range,
 *  from/to, ifilter…) and replacing a stale `tab`. */
export function interfacesTabUrl(deviceId: string, search: string | URLSearchParams): string {
  const next = new URLSearchParams(search)
  next.delete('tab')
  next.set('tab', 'interfaces')
  return `/devices/${encodeURIComponent(deviceId)}?${next.toString()}`
}
