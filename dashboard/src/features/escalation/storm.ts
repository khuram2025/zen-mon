/**
 * Alert storm (correlation) policy — pure model for the "Alert storms" tab.
 * Mirrors app.services.alert_storm: settings shape, limits, the plain-language
 * summary and the API → view mapping. Imports nothing, so
 * scripts/test-alert-storm-ui.mjs can run it on its own.
 */

export type StormFamily =
  | 'reachability' | 'reboot' | 'service' | 'interface' | 'latency'
  | 'hardware' | 'network' | 'process' | 'trap' | 'syslog'
export type StormScope = 'all' | 'site' | 'device_group' | 'scope_tag' | 'appliance'

export interface StormPolicy {
  enabled: boolean
  threshold: number
  window_minutes: number
  families: StormFamily[]
  scope: StormScope
  update_minutes: number
  hold_escalations: boolean
}

export interface StormMember {
  alert_id: string | null
  asset: string
  held: boolean
  joined_at: string | null
  recovered: boolean
  message: string | null
}

export interface Storm {
  id: string
  family: string
  family_label: string
  scope_label: string | null
  status: 'active' | 'ended'
  threshold: number
  window_minutes: number
  started_at: string | null
  last_event_at: string | null
  ended_at: string | null
  member_count: number
  recovered_count: number
  members?: StormMember[]
}

export const STORM_DEFAULTS: StormPolicy = {
  enabled: false, threshold: 5, window_minutes: 5, families: ['reachability', 'reboot'],
  scope: 'all', update_minutes: 15, hold_escalations: true,
}

export const STORM_LIMITS = {
  threshold: [2, 1000], window_minutes: [1, 120], update_minutes: [5, 240],
} as const

export const STORM_FAMILIES: { value: StormFamily; label: string; hint: string }[] = [
  { value: 'reachability', label: 'Devices down', hint: 'Device down / unreachable and server agent offline' },
  { value: 'reboot', label: 'Reboots and restarts', hint: 'Device reboots (uptime reset) and server restarts or shutdowns' },
  { value: 'service', label: 'Service checks failing', hint: 'HTTP, TCP, DNS and other service checks' },
  { value: 'interface', label: 'Interface alerts', hint: 'Interface down, errors and utilisation' },
  { value: 'latency', label: 'Latency and packet loss', hint: 'Round-trip time, jitter and loss' },
  { value: 'hardware', label: 'Hardware alerts', hint: 'Temperature, fans and power supplies' },
  { value: 'network', label: 'Routing, VPN and HA', hint: 'BGP neighbours, VPN tunnels, HA state, sessions' },
  { value: 'process', label: 'Server services and processes', hint: 'Stopped services or processes on servers' },
  { value: 'trap', label: 'SNMP traps', hint: 'Alerting SNMP traps' },
  { value: 'syslog', label: 'Syslog alerts', hint: 'Alerting syslog messages' },
]

export const STORM_SCOPES: { value: StormScope; label: string; hint: string }[] = [
  { value: 'all', label: 'All assets together', hint: 'Count affected assets across the whole appliance' },
  { value: 'site', label: 'Per site', hint: 'A storm is counted separately for each site (or location)' },
  { value: 'device_group', label: 'Per device group', hint: 'Counted separately for each device group' },
  { value: 'scope_tag', label: 'Per scope tag', hint: 'Counted separately for each set of scope tags' },
  { value: 'appliance', label: 'Per monitoring appliance', hint: 'Counted separately for the controller and each sensor' },
]

const FAMILY_SET = new Set(STORM_FAMILIES.map((f) => f.value))
const SCOPE_SET = new Set(STORM_SCOPES.map((s) => s.value))

function intIn(v: unknown, [lo, hi]: readonly [number, number], dflt: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? Math.max(lo, Math.min(hi, Math.trunc(n))) : dflt
}

export function stormPolicyFromApi(raw: Record<string, any> | null | undefined): StormPolicy {
  const r = raw ?? {}
  return {
    enabled: !!r.enabled,
    threshold: intIn(r.threshold, STORM_LIMITS.threshold, STORM_DEFAULTS.threshold),
    window_minutes: intIn(r.window_minutes, STORM_LIMITS.window_minutes, STORM_DEFAULTS.window_minutes),
    families: Array.isArray(r.families)
      ? STORM_FAMILIES.map((f) => f.value).filter((f) => r.families.includes(f))
      : [...STORM_DEFAULTS.families],
    scope: SCOPE_SET.has(r.scope) ? r.scope : 'all',
    update_minutes: intIn(r.update_minutes, STORM_LIMITS.update_minutes, STORM_DEFAULTS.update_minutes),
    hold_escalations: r.hold_escalations === undefined ? true : !!r.hold_escalations,
  }
}

export function validateStormPolicy(p: StormPolicy): Partial<Record<keyof StormPolicy, string>> {
  const errors: Partial<Record<keyof StormPolicy, string>> = {}
  const check = (key: 'threshold' | 'window_minutes' | 'update_minutes', what: string) => {
    const [lo, hi] = STORM_LIMITS[key]
    const v = p[key]
    if (!Number.isInteger(v) || v < lo || v > hi) errors[key] = `${what} must be a whole number from ${lo} to ${hi}`
  }
  check('threshold', 'Number of assets')
  check('window_minutes', 'Window')
  check('update_minutes', 'Update interval')
  if (p.enabled && p.families.length === 0) errors.families = 'Select at least one event type'
  if (p.families.some((f) => !FAMILY_SET.has(f))) errors.families = 'Unknown event type'
  return errors
}

export function stormSentence(p: StormPolicy): string {
  if (!p.enabled) return 'Off: every asset is notified on its own.'
  if (p.families.length === 0) return 'On, but no event type is selected, so nothing is combined.'
  const fams = STORM_FAMILIES.filter((f) => p.families.includes(f.value)).map((f) => f.label.toLowerCase())
  const scope = { all: 'across all assets', site: 'per site', device_group: 'per device group',
    scope_tag: 'per scope tag', appliance: 'per monitoring appliance' }[p.scope]
  return `When more than ${p.threshold} assets report ${fams.join(', ')} within ${p.window_minutes} min (${scope}), `
    + `each channel gets one combined notification instead of one per asset, then an update every `
    + `${p.update_minutes} min and a closing summary.`
}

/** What a recipient receives in a storm of `affected` assets (threshold N). */
export function stormExample(p: StormPolicy, affected: number): string {
  if (!p.enabled || affected <= p.threshold) return `${affected} individual notification${affected === 1 ? '' : 's'}`
  return `${p.threshold} individual notification${p.threshold === 1 ? '' : 's'}, then 1 combined notification `
    + `listing all ${affected} assets (instead of ${affected - p.threshold} more)`
}

export function stormFromApi(r: Record<string, any>): Storm {
  return {
    id: String(r.id ?? ''),
    family: String(r.family ?? ''),
    family_label: String(r.family_label ?? r.family ?? ''),
    scope_label: r.scope_label ? String(r.scope_label) : null,
    status: r.status === 'active' ? 'active' : 'ended',
    threshold: Number(r.threshold ?? 0),
    window_minutes: Number(r.window_minutes ?? 0),
    started_at: r.started_at ?? null,
    last_event_at: r.last_event_at ?? null,
    ended_at: r.ended_at ?? null,
    member_count: Number(r.member_count ?? 0),
    recovered_count: Number(r.recovered_count ?? 0),
    members: Array.isArray(r.members)
      ? r.members.map((m: Record<string, any>) => ({
        alert_id: m.alert_id ? String(m.alert_id) : null, asset: String(m.asset ?? ''), held: !!m.held,
        joined_at: m.joined_at ?? null, recovered: !!m.recovered, message: m.message ?? null,
      }))
      : undefined,
  }
}
