/** Types for the Servers monitoring module (/servers, /agent-fleet, /agent-policies, /server-baselines). */

export type ServerStatus = 'healthy' | 'warning' | 'critical' | 'unknown' | 'stale' | 'disabled'
export type AgentStatus = 'enrolling' | 'online' | 'stale' | 'offline' | 'disabled' | 'updating' | 'error'
export type OsType = 'windows' | 'linux' | 'macos' | 'bsd' | 'other' | 'unknown'
export type CollectionMode = 'agent' | 'agentless_wmi' | 'agentless_winrm' | 'snmp' | 'ssh' | 'none'
export type UpdateRing = 'canary' | 'beta' | 'stable' | 'pinned'
export type Severity = 'info' | 'warning' | 'critical'

export interface ServerItem {
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
  status_reasons: string[]
  agent_id: string | null
  agent_status: AgentStatus | null
  agent_version: string | null
  agent_last_heartbeat_at: string | null
  created_at: string
  updated_at: string
}

export interface ServerListResponse {
  items: ServerItem[]
  total: number
  page: number
  page_size: number
}

export interface FacetValue { value: string; count: number }
export interface ServerFacets {
  status: FacetValue[]
  os_type: FacetValue[]
  collection_mode: FacetValue[]
  environment: FacetValue[]
  tags: FacetValue[]
  sites: { id: string; name: string; count: number }[]
}

export interface ServerLiveMetrics {
  cpu_pct?: number
  memory_pct?: number
  disk_max_pct?: number
  net_bps?: number
}

export interface ServerMonitoringOverview {
  total: number
  status_counts: Partial<Record<ServerStatus, number>>
  os_counts: Partial<Record<OsType, number>>
  agent_counts: Partial<Record<AgentStatus, number>>
  sites: { id: string; name: string; server_count: number }[]
  top_cpu: TopPressureItem[]
  top_memory: TopPressureItem[]
  top_disk: TopPressureItem[]
  top_network: TopPressureItem[]
}

export interface TopPressureItem {
  server_id: string
  value: number
  display_name?: string
  hostname?: string | null
}

export interface AgentItem {
  id: string
  server_id: string | null
  server_name: string | null
  site_id: string | null
  site_name: string | null
  agent_uid: string
  hostname: string | null
  platform: string
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

export interface AgentPolicy {
  id: string
  name: string
  description: string | null
  platform: 'windows' | 'linux' | 'any'
  metric_interval_s: number
  upload_interval_s: number
  process_top_n: number
  service_watchlist: string[]
  process_watchlist: string[]
  event_log_filters: Record<string, unknown>[]
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

export interface InstallToken {
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

export interface MetricPoint { timestamp: string; value: number | null }
export interface MetricSeries {
  metric: string
  unit: string | null
  label: string | null
  points: MetricPoint[]
}
export interface ServerMetricsResponse {
  server_id: string
  from: string
  to: string
  interval_s: number
  series: MetricSeries[]
}

export interface ServerProcess {
  pid: number
  name: string
  cmdline: string | null
  user_name: string | null
  cpu_pct: number | null
  memory_bytes: number | null
  started_at: string | null
  updated_at: string
}

export interface ServerService {
  service_name: string
  display_name: string | null
  start_mode: string | null
  state: string | null
  pid: number | null
  description: string | null
  updated_at: string
}

export interface ServerFilesystem {
  mount: string
  fs_type: string | null
  device: string | null
  total_bytes: number | null
  used_bytes: number | null
  free_bytes: number | null
  used_pct: number | null
  updated_at: string
}

export interface ServerNetworkInterface {
  if_name: string
  mac_address: string | null
  ip_addresses: string[]
  speed_mbps: number | null
  is_up: boolean | null
  mtu: number | null
  updated_at: string
}

export interface ServerSoftware {
  package_name: string
  version: string | null
  vendor: string | null
  install_date: string | null
  updated_at: string
}

export interface ServerEventRow {
  timestamp: string
  log_name: string
  level: string
  count: number
}

export interface ServerCommand {
  id: string
  command: string
  params: Record<string, unknown>
  status: string
  created_at: string
  sent_at: string | null
  completed_at: string | null
  expires_at: string | null
  success: boolean | null
  error_message: string | null
  requested_by_name: string | null
}

// ── Baselines ────────────────────────────────────────────────────────

export type BaselineRuleType = 'required' | 'prohibited'
export type BaselineMatchType = 'exact' | 'contains' | 'regex'
export type ComplianceStatus = 'compliant' | 'missing' | 'outdated' | 'prohibited'

export interface BaselineRule {
  id: string
  baseline_id: string
  rule_type: BaselineRuleType
  package_match: string
  match_type: BaselineMatchType
  min_version: string | null
  severity: Severity
  notes: string | null
  created_at: string
}

export interface BaselineRuleInput {
  rule_type: BaselineRuleType
  package_match: string
  match_type: BaselineMatchType
  min_version?: string | null
  severity: Severity
  notes?: string | null
}

export interface Baseline {
  id: string
  name: string
  description: string | null
  enabled: boolean
  os_type: string | null
  site_id: string | null
  site_name: string | null
  match_tags: string[]
  alerting: boolean
  rule_count: number
  servers_evaluated: number
  servers_compliant: number
  violations: number
  rules: BaselineRule[]
  created_at: string
  updated_at: string
}

export interface ComplianceResult {
  rule_id: string
  baseline_id: string
  status: ComplianceStatus
  found_package: string | null
  found_version: string | null
  expected: string | null
  severity: Severity
  first_failed_at: string | null
  evaluated_at: string
  baseline_name?: string
  server_id?: string
  server_name?: string
  hostname?: string | null
  os_type?: string
  rule_type: BaselineRuleType
  package_match: string
  match_type: BaselineMatchType
  min_version: string | null
  notes?: string | null
}

export interface ComplianceSummary {
  total: number
  compliant: number
  missing: number
  outdated: number
  prohibited: number
}
