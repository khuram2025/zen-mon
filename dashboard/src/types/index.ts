export interface Device {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  group_id: string | null
  group_name: string | null
  tags: string[]
  ping_enabled: boolean
  ping_interval: number
  status: DeviceStatus
  last_seen: string | null
  last_rtt_ms: number | null
  description: string | null
  created_at: string
  updated_at: string
}

export type DeviceStatus = 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'

export interface DeviceSummary {
  total: number
  up: number
  down: number
  degraded: number
  unknown: number
  maintenance: number
}

export interface DeviceGroup {
  id: string
  name: string
  description: string | null
  color: string | null
  device_count: number
}

export interface Alert {
  id: string
  rule_id: string | null
  device_id: string | null
  device_hostname: string | null
  device_ip: string | null
  service_check_id: string | null
  service_check_name: string | null
  status: 'active' | 'acknowledged' | 'resolved'
  severity: 'info' | 'warning' | 'critical'
  message: string
  triggered_at: string
  acknowledged_at: string | null
  resolved_at: string | null
}

export interface AlertStats {
  active: number
  acknowledged: number
  resolved_today: number
  critical: number
  warning: number
  info: number
}

export interface MetricPoint {
  timestamp: string
  rtt_ms: number | null
  packet_loss: number | null
  jitter_ms: number | null
  min_rtt_ms: number | null
  max_rtt_ms: number | null
  is_up: boolean | null
}

export interface MetricResponse {
  device_id: string
  granularity: string
  from_time: string
  to_time: string
  points: MetricPoint[]
}

export interface PaginatedResponse<T> {
  data: T[]
  meta: {
    total: number
    skip: number
    limit: number
  }
}

export interface User {
  id: string
  username: string
  email: string
  full_name: string | null
  role: string
  is_active?: boolean
  last_login: string | null
  created_at?: string
  updated_at?: string
}

export interface Role {
  id: string
  name: string
  description: string
  permissions: string[]
  user_count: number
}

export interface SubscriptionInfo {
  id: string
  plan: string
  status: string
  started_at: string
  expires_at: string
  max_devices: number
  max_service_checks: number
  max_users: number
  license_key: string | null
  activated_by: string | null
  days_remaining: number
  usage: {
    devices: number
    service_checks: number
    users: number
  }
  features: string[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export type ServiceCheckType = 'http' | 'tcp' | 'tls' | 'icmp' | 'dns'
export type ServiceStatus = 'up' | 'down' | 'degraded' | 'warning' | 'unknown'
export type ServiceLevel = 1 | 2 | 3

export interface ServiceCheckGroup {
  id: string
  name: string
  description: string | null
  color: string | null
  check_count: number
  created_at: string
  updated_at: string | null
}

export interface ServiceCheckTemplate {
  id: string
  name: string
  description: string | null
  check_type: ServiceCheckType
  level: ServiceLevel
  config: Record<string, unknown>
  tags: string[]
  default_interval: number
  default_timeout: number
  default_retry_count: number
  default_retry_delay_s: number
  target_url_template: string | null
  target_port_default: number | null
  http_method: string | null
  http_expected_status: number | null
  http_expected_statuses: string | null
  http_content_match: string | null
  http_follow_redirects: boolean | null
  tls_warn_days: number | null
  tls_critical_days: number | null
  created_at: string
  updated_at: string | null
}

export interface ServiceMaintenanceWindow {
  id: string
  scope_type: 'check' | 'group' | 'tag' | 'all'
  scope_check_id: string | null
  scope_group_id: string | null
  scope_tag: string | null
  scope_label: string | null
  starts_at: string
  ends_at: string
  reason: string | null
  active: boolean
  created_at: string
}

export interface ServiceCheck {
  id: string
  device_id: string | null
  device_hostname: string | null
  group_id: string | null
  group_name: string | null
  parent_check_id?: string | null
  parent_check_name?: string | null
  name: string
  check_type: ServiceCheckType
  level: ServiceLevel
  config: Record<string, unknown>
  tags: string[]
  retry_count?: number
  retry_delay_s?: number
  in_maintenance?: boolean
  enabled: boolean
  target_host: string
  target_port: number | null
  target_url: string | null
  http_method: string
  http_expected_status: number
  http_expected_statuses: string | null
  http_content_match: string | null
  http_follow_redirects: boolean
  tls_warn_days: number
  tls_critical_days: number
  check_interval: number
  timeout: number
  status: ServiceStatus
  last_check_at: string | null
  last_response_ms: number | null
  last_error: string | null
  tls_expiry_date: string | null
  tls_days_remaining: number | null
  tls_issuer: string | null
  tls_subject: string | null
  description: string | null
  created_at: string
  updated_at: string | null
}

export interface ServiceCheckSummary {
  total: number
  up: number
  down: number
  warning: number
  degraded: number
  unknown: number
}

export interface ServiceMetricPoint {
  timestamp: string
  response_ms: number | null
  is_up: boolean | null
  status_code: number | null
  tls_days_remaining: number | null
  error_message: string | null
}

export interface ServiceMetricResponse {
  service_check_id: string
  granularity: string
  from_time: string
  to_time: string
  points: ServiceMetricPoint[]
}
