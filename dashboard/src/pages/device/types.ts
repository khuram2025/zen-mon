import type { DeviceAvailabilityPoint } from '@/components/devices/availability'

/* Shapes shared by the device page shell and its tabs. */

export type DeviceRange = { hours: number; fromISO: string; toISO: string; isCustom: boolean; label: string }

export type MetricSeriesMap = Record<string, { unit?: string; points: { ts: number; value: number }[] }>

export type PingPoint = DeviceAvailabilityPoint & { rtt_ms: number | null; packet_loss: number | null; jitter_ms: number | null }

export type StatusHistoryEvent = {
  old_status: string
  new_status: string
  reason?: string
  timestamp: string
  duration_sec?: number | null
}

export type Severity = 'critical' | 'warning' | 'info'

export type DeviceIssue = {
  source: 'status' | 'flapping' | 'alert' | 'interfaces' | 'template' | 'maintenance'
  severity: Severity
  title: string
  detail: string | null
  since: string | null
  until?: string | null
  tab: string
  group?: string
  alert_id?: string
  /** Alert issues standing for repeats of one condition: how many, the newest, and the rule. */
  count?: number
  last_at?: string | null
  rule_id?: string | null
}

export type IdentityField = { value: string | null; source: 'inventory' | 'device' | 'template' | 'entity' | null }

export type HealthGroupDigest = {
  key: string
  name: string
  kind: 'scalar' | 'table'
  status: 'ok' | 'info' | 'warn' | 'crit' | 'none' | 'na'
  summary: string
  attention: number
  total: number
  /** The template-declared vendor tab that shows this group (null = default vendor tab or Hardware). */
  tab?: string | null
}

export type InterfaceCounts = {
  total: number
  up: number
  unexpected_down: number
  admin_down: number
  not_present: number
  unused: number
  unmonitored_down: number
  other: number
  unexpected: Array<{ if_index: number; name: string; alias: string | null }>
}

export type BusyInterface = {
  if_index: number
  name: string
  alias: string | null
  speed_bps: number | null
  in_bps: number
  out_bps: number
  util_pct: number | null
  wan: boolean
  oper_status: string
  sampled_at: number
}

/** ``title`` is the triggering condition without the repeated hostname and IP ("Packet loss 33% (limit 10%)"). */
export type SummaryAlert = {
  id: string; severity: string; status: string; message: string; title?: string; triggered_at: string | null
  rule_id?: string | null; rule_name?: string | null; group_key?: string | null
}

/** Open alerts that repeat one condition from one rule (D-09); count 1 is a single alert. Members newest first. */
export type SummaryAlertGroup = {
  key: string; label: string; count: number; severity: string; rule_id: string | null; rule_name: string | null
  latest_title: string; first_at: string | null; last_at: string | null; active: number; acknowledged: number
  members: Array<{ id: string; title: string; status: string; severity: string; triggered_at: string | null }>
}

export type TemplateHighlight = { kind: 'sessions' | 'clients' | 'poe'; label: string; value: number; unit: string | null; sub: string | null; series_key: string | null }

export type FlapEpisodeLite = {
  id: string
  started_at: string
  ended_at: string | null
  ongoing: boolean
  kind: string | null
  start_changes: number | null
  window_minutes: number | null
  status_at_start: string | null
  status_at_end: string | null
  end_reason: string | null
}

export type DeviceSummary = {
  device_id: string
  status: string
  status_changed_at: string | null
  flapping: boolean
  identity: { model: IdentityField; os_version: IdentityField; firmware: IdentityField; serial_number: IdentityField }
  template: { name: string; vendor: string | null; updated_at: string | null; tabs?: Array<{ id: string; label: string; groups: string[] }> } | null
  health: { state: 'ok' | 'warning' | 'critical'; issue_count: number; critical: number; warning: number; groups: HealthGroupDigest[] }
  kpis: { cpu_thresholds: Thresholds | null; memory_thresholds: Thresholds | null; highlights: TemplateHighlight[] }
  issues: DeviceIssue[]
  not_applicable: Array<{ key: string; name: string; reason: string | null }>
  interfaces: InterfaceCounts | null
  busiest_interfaces: BusyInterface[] | null
  alerts: { active: number; acknowledged: number; items: SummaryAlert[]; groups?: SummaryAlertGroup[] } | null
  maintenance: { active: boolean; ends_at?: string | null; reason?: string | null }
  recent: { status_changes: StatusHistoryEvent[] | null; traps: TrapRow[] | null; flap_episodes: FlapEpisodeLite[] | null }
  polling: { ping_interval_s: number | null; snmp_interval_s: number | null; last_response: string | null }
  unavailable: string[]
  generated_at: string
}

export type Thresholds = { warn?: number | null; crit?: number | null; op?: string | null }

export type TrapRow = { trap_oid?: string; trap_name?: string; severity?: string; message?: string; timestamp: string; bindings?: unknown }

/** The device record (GET /devices/{id}); only the fields the page reads. */
export type DeviceRecord = {
  id: string
  hostname: string
  ip_address: string | null
  managed_ip?: string | null
  device_type: string
  vendor: string | null
  model: string | null
  location: string | null
  group_name: string | null
  tags: string[]
  status: string
  status_changed_at: string | null
  last_seen: string | null
  ping_enabled: boolean
  ping_interval: number
  snmp_enabled: boolean
  snmp_version: string
  snmp_port: number
  profile_name: string | null
  sys_object_id: string | null
  os_version: string | null
  serial_number: string | null
  firmware_version: string | null
  uptime_seconds?: number | null
  managed_by_device_id?: string | null
  managed_by_hostname?: string | null
  is_flapping?: boolean
  flapping_since?: string | null
  flap_kind?: string | null
  updated_at?: string | null
  /** Who looks after the device and operational notes (free text; null = none recorded). */
  owner?: string | null
  notes?: string | null
  description?: string | null
  [key: string]: unknown
}
