/* GET /devices/{id}/controller-readings and /controller-history: the readings a
 * controller (FortiGate) reports for a managed FortiSwitch or FortiAP, and the
 * wording the device page uses for them. Kept free of React so
 * scripts/test-controller-children.mjs can check it. */

export type EnumReading = { code: number | null; text: string | null; sev: string | null; status?: string | null }

export type ControllerAvailability = {
  pct: number | null
  coverage_pct: number
  sufficient: boolean
  samples: number
  basis: 'controller'
}

export type NotSupported = { metric: string; label: string; reason: string; requires: string }

export type SwitchPort = {
  instance: string
  name: string
  number: number | null
  link: EnumReading | null
  up: boolean
  speed: string | null
  native_vlan: string | null
  allowed_vlans: string | null
  untagged_vlans: string | null
  poe: { capable: boolean | null; enabled: boolean | null; state: string | null; draw_w: number | null }
  connected_devices: Array<{ id: string; hostname: string; status: string; model: string | null }>
  measured_at: string | null
}

export type SwitchPanel = {
  supported: true
  source: string
  measured_at: string | null
  stale: boolean
  summary: { ports: number; up: number; poe_capable: number; poe_draw_w: number | null; poe_budget_w: number | null }
  ports: SwitchPort[]
  vlans: Array<{ vlan: string; native_ports: string[]; member_ports: number }>
  note: string | null
}

export type ApRadio = {
  radio_id: number
  mode: EnumReading | null
  bssid: string | null
  channel: number | null
  band: string | null
  phy: string | null
  width: string | null
  configured_channels: string | null
  tx_power_dbm: number | null
  clients: number | null
  utilization_pct: number | null
  signal: { avg_dbm: number; min_dbm: number; poor_clients: number } | null
  ssids: string[]
}

export type WirelessClient = {
  radio_id: number
  ssid: string | null
  signal_dbm: number | null
  signal_quality: 'excellent' | 'good' | 'fair' | 'poor' | null
  noise_dbm: number | null
  channel: number | null
  vlan: string | null
  tx_kbps: number | null
  rx_kbps: number | null
  /** Identity: only with udt.view (user with udt.view_users). */
  mac?: string | null
  ip?: string | null
  host?: string | null
  user?: string | null
}

export type ApPanel = {
  supported: true
  source: string
  measured_at: string | null
  stale: boolean
  summary: { radios: number; ssids: number; clients: number | null; client_rows: number; clients_truncated: boolean;
    rx_bps: number | null; tx_bps: number | null }
  radios: ApRadio[]
  ssids: Array<{ ssid: string; radio_id: number; if_index: number; clients: number | null; rx_bps: number | null; tx_bps: number | null }>
  clients_by_ssid: Array<{ ssid: string; clients: number }>
  clients: WirelessClient[]
  clients_restricted: boolean
  users_restricted: boolean
  uplink: {
    capwap: EnumReading | null
    session_uptime_s: number | null
    ip: string | null
    switch_ports: Array<{ switch_serial: string; port_number: number | null; if_name: string | null; vlan_id: number | null;
      is_direct: boolean; last_seen: string | null; switch: { id: string; hostname: string; status: string; model: string | null } | null }>
  }
}

export type ControllerReadings = {
  device_id: string
  managed: boolean
  kind?: 'switch' | 'access_point' | string
  controller?: { id: string; hostname: string; status: string; model: string | null }
  source_group?: string
  measured_at?: string | null
  stale?: boolean
  stale_after_s?: number
  age_s?: number | null
  reported?: boolean
  readings?: {
    cpu: number | null
    memory: number | null
    uptime_s: number | null
    clients: number | null
    state: EnumReading | null
    authorization: EnumReading | null
    model: string | null
    firmware: string | null
    serial: string | null
    ip: string | null
    mac: string | null
    rx_bps: number | null
    tx_bps: number | null
    session_uptime_s: number | null
    joined_at: string | null
    flags: string | null
    offline: boolean
    offline_text: string | null
  }
  availability?: ControllerAvailability | null
  panel?: SwitchPanel | ApPanel | { supported: false; reason: string } | null
  not_supported?: NotSupported[]
  unavailable?: string[]
}

export type ControllerHistory = {
  hours: number
  step_s?: number
  controller_id?: string
  series: Array<{ key: string; radio_id?: number; points: Array<{ ts: string; value: number }> }>
  unavailable?: string[]
}

export function isSwitchPanel(p: ControllerReadings['panel']): p is SwitchPanel {
  return !!p && p.supported && 'ports' in p
}

export function isApPanel(p: ControllerReadings['panel']): p is ApPanel {
  return !!p && p.supported && 'radios' in p
}

/** "Reported by SNIH-FGT 40 s ago" / "… · stale (limit 10 min)". */
export function sourceText(r: Pick<ControllerReadings, 'controller' | 'age_s' | 'stale' | 'stale_after_s' | 'reported'>): string {
  const who = r.controller?.hostname || 'the controller'
  if (!r.reported) return `Not reported by ${who} yet`
  const age = r.age_s == null ? '' : ` ${ageText(r.age_s)} ago`
  const stale = r.stale ? ` · stale (older than ${ageText(r.stale_after_s ?? 600)})` : ''
  return `Reported by ${who}${age}${stale}`
}

export function ageText(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.round(s / 60)} min`
  if (s < 86400) return `${Math.round(s / 3600)} h`
  return `${Math.round(s / 86400)} d`
}

/** Availability headline: a percentage only with enough controller-observed history. */
export function availabilityText(a: ControllerAvailability | null | undefined): { value: string; sub: string } {
  if (!a) return { value: '—', sub: 'No controller-reported state yet' }
  if (!a.sufficient || a.pct == null) {
    return { value: 'Insufficient history', sub: `${a.coverage_pct.toFixed(0)}% of the last 24 h observed by the controller` }
  }
  return { value: `${a.pct.toFixed(2)}%`, sub: `Controller-reported state · ${a.coverage_pct.toFixed(0)}% of 24 h observed` }
}

/** "port3, port7 +2" for a list of port names. */
export function portList(names: string[], max = 3): string {
  if (!names.length) return '—'
  return names.slice(0, max).join(', ') + (names.length > max ? ` +${names.length - max}` : '')
}

export function signalTone(quality: WirelessClient['signal_quality']): 'ok' | 'warn' | 'crit' | 'none' {
  return quality === 'excellent' || quality === 'good' ? 'ok' : quality === 'fair' ? 'warn' : quality === 'poor' ? 'crit' : 'none'
}

/** Watts with two decimals at most: "6.2 W". */
export function formatWatts(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${Math.round(value * 100) / 100} W`
}

/** Points for a TimeSeriesPanel from one history series. */
export function historyPoints(h: ControllerHistory | undefined, key: string): Array<{ ts: number; value: number | null }> {
  const s = h?.series.find((x) => x.key === key)
  return (s?.points || []).map((p) => ({ ts: Date.parse(p.ts), value: p.value }))
}
