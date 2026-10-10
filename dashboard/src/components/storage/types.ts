// Mirrors GET /system/storage and GET /system/storage/overview.

export interface MountInfo {
  mount: string
  label: string
  roles: ('system' | 'data')[]
  total_bytes: number
  used_bytes: number
  free_bytes: number
  usage_percent: number
  reserved_bytes: number
}

export interface Consumer {
  id: string
  label: string
  bytes: number
  human: string
  mount: string
  hint: string | null
}

export interface ManagedTable {
  table: string
  category: string
  time_column: string
  partition_granularity: string
  size_bytes: number
  size_human: string
  rows: number
  oldest: string | null
  newest: string | null
  current_ttl_days: number | null
}

export interface RetentionCategory {
  id: string
  label: string
  description: string
  default_days: number
  min_days: number
  configured_days: number | null
  size_bytes: number
  size_human: string
  rows: number
  daily_bytes: number
  projected_bytes: number
  unbounded: boolean
  tables: ManagedTable[]
}

export interface AutoPurge {
  enabled: boolean
  threshold_pct: number
  target_pct: number
  min_keep_days: number
  min_free_gib: number
}

export interface BackupSchedule {
  enabled: boolean
  frequency: 'daily' | 'weekly'
  weekday: number
  hour_utc: number
  include_clickhouse: boolean
  keep_last: number
}

export interface Forecast {
  daily_bytes: number
  current_bytes: number
  steady_state_bytes: number
  growth_remaining_bytes: number
  free_bytes: number
  fits: boolean
  days_until_full: number | null
  unbounded_categories: string[]
}

export interface ClickHouseTable {
  table: string
  database: string
  active_bytes: number
  inactive_bytes: number
  parts: number
}

export interface ClickHouseBreakdown {
  at: number
  application_bytes: number
  diagnostic_bytes: number
  inactive_bytes: number
  detached_bytes: number
  top_tables: ClickHouseTable[]
  growth: { table: string; bytes_per_hour: number; bytes: number }[]
  source: 'watchdog' | 'live'
}

export interface DiagnosticLog {
  expires_after?: string | null
  table: string
  log: string
  kind: 'current' | 'legacy' | 'evidence'
  evidence: boolean
  disabled: boolean
  legacy: boolean
  protected: boolean
  retention_days: number
  ttl_active: boolean
  bytes: number
}

export interface WatchdogFs {
  minimum_free_bytes?: number
  critical_free_bytes?: number
  mount: string
  total_bytes: number
  used_bytes: number
  avail_bytes: number
  reserved_bytes: number
  usage_pct: number
  state: 'ok' | 'growth' | 'pressure' | 'critical'
  reasons: string[]
  need_bytes: number
  runway_hours: number | null
  growth_bytes_per_hour: number | null
  exhausted: boolean
  insufficient?: boolean
}

export interface WatchdogStatus {
  installed: boolean
  alive: boolean
  heartbeat?: number
  heartbeat_age_s?: number
  state?: WatchdogFs['state']
  filesystems?: WatchdogFs[]
  pending_bytes?: number
  ingest_paused?: boolean
  last_pass?: { at: number; scheduled_bytes: number; action_count: number; errors: string[]; reason: any } | null
  last_error?: { at: number; message: string } | null
  history?: { t: number; fs: Record<string, [number, number]> }[]
}

export interface Overview {
  mounts: MountInfo[]
  data_shares_os: boolean
  sampled_at: string
  journal_bytes: number | null
  var_log_bytes: number | null
  apt_cache_bytes: number | null
  os_usage_error?: string | null
  consumers: Consumer[]
  clickhouse: ClickHouseBreakdown | null
  diagnostic_logs: DiagnosticLog[]
  watchdog: WatchdogStatus
  clickhouse_error: string | null
  forecast: Forecast
  categories: RetentionCategory[]
  auto_purge: AutoPurge
  table_overrides: Record<string, number>
  backup_schedule: BackupSchedule
}

export interface PvRemoval {
  action: 'remove' | 'evacuate' | null
  reason: string | null
}

export interface PhysicalVolume {
  path: string
  vg: string | null
  in_data_vg: boolean
  size_bytes: number
  used_bytes: number
  free_bytes: number
  claimable_bytes: number
  removal: PvRemoval
}

export type DiskStatus = 'system' | 'pool' | 'unused' | 'needs_wipe' | 'foreign' | 'in_use' | 'read_only'

export interface Disk {
  name: string
  path: string
  size_bytes: number
  model: string | null
  serial: string | null
  transport: string | null
  rotational: boolean
  read_only: boolean
  status: DiskStatus
  label: string
  detail: string | null
  can_add: boolean
  needs_wipe: boolean
  pvs: PhysicalVolume[]
  partitions: { name: string; path: string; size_bytes: number; fstype: string | null; mountpoint: string | null }[]
}

export interface Volume {
  vg: string | null
  lv: string | null
  path: string
  size_bytes: number
  on_lvm: boolean
  fstype: string | null
  mount_point: string
  roles: ('system' | 'data')[]
  usage: { total_bytes: number; used_bytes: number; free_bytes: number; usage_percent: number } | null
}

export interface VolumeGroup {
  name: string
  size_bytes: number
  free_bytes: number
  extent_bytes: number
  pv_count: number
  missing_pv_count: number
  is_data: boolean
}

export interface Layout {
  available: boolean
  errors: string[]
  data_vg: string | null
  data_volume: Volume | null
  data_shares_os: boolean
  volumes: Volume[]
  volume_groups: VolumeGroup[]
  disks: Disk[]
  missing_pvs: { uuid: string; vg: string; size_bytes: number; used_bytes: number }[]
  claimable: { pv: string; disk: string; bytes: number }[]
  pvmove: { pv: string; vg: string; percent: number } | null
  unallocated_bytes: number
  extent_bytes: number
}

export type Severity = 'critical' | 'warning' | 'notice' | 'info'

export interface StorageIssue {
  severity: Severity
  code: string
  title: string
  detail: string | null
  action: 'expand' | 'claim' | 'extend' | 'add' | 'rescan' | 'remove_missing' | null
}

export interface StorageStatus {
  health: 'ok' | 'warning' | 'critical'
  health_message: string
  clickhouse_storage: { ok: boolean; message: string }
  layout: Layout
  issues: StorageIssue[]
}

export interface Backup {
  id: string
  created_at: string
  created_by: string
  kind: 'config' | 'full'
  status: 'running' | 'completed' | 'failed'
  include_clickhouse: boolean
  size_bytes: number
  size_human: string
  note: string | null
  error: string | null
  last_restore_at: string | null
  last_restore_status: 'running' | 'completed' | 'failed' | null
  last_restore_error: string | null
}

export interface StorageEvent {
  id: number
  created_at: string
  event_type: string
  actor: string
  freed_bytes: number
  freed_human: string
  details: Record<string, any>
}

export type StorageSection = 'overview' | 'disks' | 'retention' | 'backups' | 'activity'
