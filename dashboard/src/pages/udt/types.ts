export type EndpointType =
  | 'workstation' | 'server' | 'phone' | 'printer' | 'access_point'
  | 'camera' | 'virtual' | 'network' | 'iot' | 'unknown'

export interface UdtSummary {
  total_endpoints: number
  active_endpoints: number
  new_24h: number
  rogue: number
  watched: number
  randomized: number
  switches: number
  logins_24h: number
  total_ports: number
  used_ports: number
  port_utilization_pct: number
  top_switches: { id: string; hostname: string; total: number; used: number; pct: number }[]
}

export interface Endpoint {
  id: string
  mac: string
  vendor: string | null
  hostname: string | null
  ip: string | null
  endpoint_type: EndpointType
  is_randomized: boolean
  is_watched: boolean
  authorized: boolean | null
  ignored: boolean
  user_name: string | null
  user_domain: string | null
  device_id: string | null
  first_seen: string
  last_seen: string
  loc_device_id?: string | null
  switch_hostname?: string | null
  if_index?: number | null
  if_name?: string | null
  vlan_id?: number | null
  is_direct?: boolean | null
  online?: boolean
}

export interface EndpointList {
  data: Endpoint[]
  meta: { total: number; skip: number; limit: number }
}

export interface EndpointLocation {
  id: number
  device_id: string
  switch: string
  if_index: number
  if_name: string | null
  if_alias: string | null
  vlan_id: number | null
  is_direct: boolean
  active: boolean
  first_seen: string
  last_seen: string
  closed_at: string | null
}

export interface EndpointDetail {
  endpoint: Endpoint & { notes: string | null; user_seen_at: string | null; managed_hostname: string | null }
  locations: EndpointLocation[]
  ip_history: { ip: string; source: string; active: boolean; first_seen: string; last_seen: string }[]
  logins: { user_name: string; user_domain: string | null; event_id: number | null; logon_type: number | null; ip: string | null; hostname: string | null; event_time: string }[]
  events: { event_type: string; device_id: string | null; switch: string | null; if_index: number | null; details: any; created_at: string }[]
}

export interface UdtPort {
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_type: number | null
  admin_status: string | null
  oper_status: string | null
  if_speed: number | null
  is_uplink: boolean | null
  uplink_reason: string | null
  uplink_override: string | null
  monitored: boolean | null
  mac_count: number | null
  vlan_ids: number[]
  pvid: number | null
  last_endpoint_seen: string | null
  active_endpoints: number
}

export interface UdtRule {
  id: string
  list_type: 'watch' | 'allow' | 'ignore'
  match_type: string
  pattern: string
  description: string | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface CapacityRow {
  id: string
  hostname: string
  location: string | null
  total: number
  used: number
  free: number
  uplinks: number
  active: number
  pct: number
}

export interface UdtEvent {
  id: number
  event_type: string
  endpoint_id: string | null
  mac: string | null
  hostname: string | null
  device_id: string | null
  switch: string | null
  if_index: number | null
  details: any
  created_at: string
}

export interface UdtGlobalSettings {
  poll_interval_s: number
  devices_total: number
  devices_enabled: number
}

export interface UdtCredentialOption {
  id: string
  name: string
  snmp_version: string
}

export interface UdtDeviceSettings {
  device_id: string
  hostname: string
  ip: string | null
  vendor: string | null
  model: string | null
  device_type: string
  is_l2: boolean
  enabled: boolean
  snmp_credential_id: string | null
  credential_name: string | null
  poll_interval_s: number | null
  ports_total: number
  ports_monitored: number
  active_endpoints: number
  last_udt_at: string | null
}

export interface DomainController {
  id: string
  name: string
  host: string
  windows_credential_id: string | null
  credential_name: string | null
  enabled: boolean
  poll_interval_s: number
  last_poll_at: string | null
  last_status: string | null
  last_error: string | null
  last_event_time: string | null
}
