// Shared types for the Discovery v2 module.

export type ScopeType = 'single_ip' | 'ip_range' | 'cidr' | 'multi' | 'csv'
export type ImportMode = 'review' | 'auto_match' | 'ignore_match'
export type ScheduleType = 'once_now' | 'once_future' | 'recurring' | 'cron'
export type Frequency = 'hourly' | 'daily' | 'weekly' | 'monthly' | 'custom'
export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'partial'
export type ResultStatus =
  | 'new'
  | 'existing'
  | 'changed'
  | 'unknown'
  | 'ignored'
  | 'failed'
  | 'imported'
export type CredentialStatus =
  | 'valid'
  | 'invalid'
  | 'not_tested'
  | 'partial'
  | 'permission_issue'

export type DiscoveryProtocol =
  | 'icmp'
  | 'snmp'
  | 'ssh'
  | 'wmi'
  | 'winrm'
  | 'http'
  | 'https'
  | 'tcp'

export interface DiscoveryProfile {
  id: string
  name: string
  description: string | null
  enabled: boolean

  scope_type: ScopeType
  targets: string[]
  exclusions: string[]
  collector_id: string | null

  protocols: DiscoveryProtocol[]
  custom_ports: number[]
  snmp_credential_ids: string[]
  windows_credential_ids: string[]
  detect_lldp: boolean
  detect_mac: boolean
  detect_vendor: boolean

  max_concurrency: number
  scan_timeout_ms: number
  retry_count: number
  rate_limit_pps: number
  max_duration_sec: number

  import_mode: ImportMode
  default_group_id: string | null
  default_tags: string[]
  default_template_id: string | null
  default_location: string | null
  default_owner: string | null
  enable_monitoring: boolean
  keep_disabled: boolean
  notify_recipients: string[]

  last_run_id: string | null
  created_by: string | null
  created_at: string
  updated_at: string

  // Summary
  last_run_status: RunStatus | null
  last_run_at: string | null
  next_run_at: string | null
  total_devices_found: number
  new_devices_found: number
  existing_devices_matched: number
  failed_targets: number
  schedule_id: string | null
  schedule_summary: string | null
}

export interface DiscoverySchedule {
  id: string
  profile_id: string
  enabled: boolean
  schedule_type: ScheduleType
  frequency: Frequency | null
  cron_expression: string | null
  interval_minutes: number | null
  time_of_day: string | null
  day_of_week: number | null
  day_of_month: number | null
  timezone: string
  start_date: string | null
  end_date: string | null
  maintenance_window: any | null
  next_run_at: string | null
  last_run_at: string | null
  last_run_id: string | null
  created_at: string
  updated_at: string
}

export interface DiscoveryRun {
  id: string
  profile_id: string
  profile_name: string | null
  schedule_id: string | null
  trigger_type: 'manual' | 'scheduled' | 'api' | 'retry'
  status: RunStatus
  phase: string
  progress_pct: number

  total_targets: number
  completed_targets: number
  responding_targets: number
  failed_targets: number
  new_devices: number
  existing_devices: number
  changed_devices: number
  unknown_devices: number
  ignored_devices: number
  credential_failures: number
  duplicate_candidates: number
  ready_to_import: number

  activity_log: Array<{ ts: string; level: string; msg: string }>
  error_details: string | null
  started_by: string | null
  started_at: string | null
  completed_at: string | null
  duration_ms: number | null
  created_at: string
}

export interface DiscoveryResult {
  id: number
  run_id: string
  profile_id: string
  ip_address: string
  mac_address: string | null
  hostname: string | null
  fqdn: string | null
  sys_name: string | null
  sys_object_id: string | null
  serial_number: string | null
  vendor: string | null
  device_type: string | null
  model: string | null
  os: string | null
  os_version: string | null
  protocols_detected: string[]
  open_ports: number[]
  response_time_ms: number | null
  credential_status: CredentialStatus
  credential_used: string | null
  status: ResultStatus
  matched_device_id: string | null
  matched_template_id: string | null
  suggested_group_id: string | null
  suggested_tags: string[]
  confidence_score: number
  conflict_type: string | null
  conflict_with_id: string | null
  import_ready: boolean
  imported: boolean
  imported_at: string | null
  imported_device_id: string | null
  ignored: boolean
  error_message: string | null
  scanned_at: string
}

export interface IgnoredDevice {
  id: string
  ip_address: string | null
  mac_address: string | null
  hostname: string | null
  reason: string | null
  ignored_at: string
}

export interface ImportBatch {
  id: string
  run_id: string
  profile_id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'partial'
  total_items: number
  successful_items: number
  failed_items: number
  skipped_items: number
  started_at: string | null
  completed_at: string | null
}

export type ImportTarget = 'auto' | 'device' | 'server' | 'both'

export interface ImportResponse {
  batch_id: string
  total: number
  successful: number
  failed: number
  skipped: number
  conflicts: number
  devices_created: number
  servers_created: number
  items: Array<{
    result_id: number
    status: string
    device_id?: string
    server_id?: string
    ip?: string
    error?: string
    reason?: string
  }>
}
