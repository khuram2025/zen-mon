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
  device_id: string
  device_hostname: string | null
  device_ip: string | null
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
  last_login: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}
