/** Types for the Server Monitoring feature.
 *
 * Backed by /api/v1/servers, /api/v1/agent-policies, /api/v1/agent-fleet,
 * /api/v1/server-monitoring/overview.
 */

export type OsType = 'windows' | 'linux' | 'macos' | 'bsd' | 'other' | 'unknown'
export type CollectionMode =
  | 'agent'
  | 'agentless_wmi'
  | 'agentless_winrm'
  | 'snmp'
  | 'ssh'
  | 'none'
export type ServerStatus =
  | 'healthy'
  | 'warning'
  | 'critical'
  | 'unknown'
  | 'stale'
  | 'disabled'
export type AgentStatus =
  | 'enrolling'
  | 'online'
  | 'stale'
  | 'offline'
  | 'disabled'
  | 'updating'
  | 'error'
export type UpdateRing = 'canary' | 'beta' | 'stable' | 'pinned'

export type Server = {
  id: string
  display_name: string
  hostname: string | null
  fqdn: string | null
  primary_ip: string | null
  site_id: string | null
  site_name: string | null
  device_id: string | null
  os_type: OsType
  os_name: string | null
  os_version: string | null
  kernel_or_build: string | null
  architecture: string | null
  collection_mode: CollectionMode
  status: ServerStatus
  environment: string | null
  owner: string | null
  tags: string[]
  last_seen: string | null
  description: string | null
  agent_id: string | null
  agent_status: AgentStatus | null
  agent_version: string | null
  created_at: string
  updated_at: string
}

export type ServerListResponse = {
  items: Server[]
  total: number
  page: number
  page_size: number
}

export type AgentPolicy = {
  id: string
  name: string
  description: string | null
  platform: 'windows' | 'linux' | 'any'
  metric_interval_s: number
  upload_interval_s: number
  process_top_n: number
  service_watchlist: string[]
  process_watchlist: string[]
  event_log_filters: Array<Record<string, unknown>>
  disk_ignore: string[]
  network_ignore: string[]
  cardinality_limits: Record<string, unknown>
  update_ring: UpdateRing
  feature_flags: Record<string, unknown>
  config_version: number
  is_builtin: boolean
  agent_count: number
  created_at: string
  updated_at: string
}

export type Agent = {
  id: string
  server_id: string | null
  server_name: string | null
  site_id: string | null
  site_name: string | null
  agent_uid: string
  hostname: string | null
  platform: 'windows' | 'linux' | 'macos' | 'other'
  version: string | null
  status: AgentStatus
  api_key_prefix: string | null
  last_heartbeat_at: string | null
  last_metric_at: string | null
  last_config_hash: string | null
  queue_depth: number
  spool_bytes: number
  update_ring: UpdateRing
  desired_version: string | null
  current_version: string | null
  certificate_expires_at: string | null
  last_ip: string | null
  policy_id: string | null
  policy_name: string | null
  config_apply_error: string | null
  tags: string[]
  created_at: string
  updated_at: string
}

export type InstallToken = {
  token_id: string
  enrollment_token: string
  token_prefix: string
  expires_at: string
  max_uses: number
  server_url: string
  platform: string
  site_id: string | null
  policy_id: string | null
  install_command: string
  msi_download_url: string | null
}

export type ServerMetricPoint = { timestamp: string; value: number | null }
export type ServerMetricSeries = {
  metric: string
  unit: string | null
  label: string | null
  points: ServerMetricPoint[]
}
export type ServerMetricsResponse = {
  server_id: string
  from: string
  to: string
  interval_s: number
  series: ServerMetricSeries[]
}

export type ServerProcess = {
  pid: number
  name: string
  cmdline: string | null
  user_name: string | null
  cpu_pct: number | null
  memory_bytes: number | null
  started_at: string | null
  updated_at: string
}

export type ServerService = {
  service_name: string
  display_name: string | null
  start_mode: string | null
  state: string | null
  pid: number | null
  description: string | null
  updated_at: string
}

export type ServerFilesystem = {
  mount: string
  fs_type: string | null
  device: string | null
  total_bytes: number | null
  used_bytes: number | null
  free_bytes: number | null
  used_pct: number | null
  updated_at: string
}

export type ServerNetworkInterface = {
  if_name: string
  mac_address: string | null
  ip_addresses: string[]
  speed_mbps: number | null
  is_up: boolean
  mtu: number | null
  updated_at: string
}

export type ServerEventLog = {
  timestamp: string
  log_name: string
  level: string
  count: number
}

export type ServerOverview = {
  total: number
  status_counts: Record<ServerStatus | string, number>
  os_counts: Record<OsType | string, number>
  agent_counts: Record<AgentStatus | string, number>
  sites: Array<{ id: string; name: string; server_count: number }>
  top_cpu: Array<{ server_id: string; value: number; display_name?: string; hostname?: string }>
  top_memory: Array<{ server_id: string; value: number; display_name?: string; hostname?: string }>
  top_disk: Array<{ server_id: string; value: number; display_name?: string; hostname?: string }>
  top_network: Array<{ server_id: string; value: number; display_name?: string; hostname?: string }>
}
