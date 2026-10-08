// Mirrors GET /system/storage and GET /system/storage/overview.

export interface MountInfo {
  mount: string
  label: string
  roles: ('system' | 'data')[]
  total_bytes: number
  used_bytes: number
  free_bytes: number
  usage_percent: number
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

export interface Overview {
  mounts: MountInfo[]
  data_shares_os: boolean
  journal_bytes: number
  apt_cache_bytes: number
  consumers: Consumer[]
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
