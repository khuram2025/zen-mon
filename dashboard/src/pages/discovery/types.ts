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
  ssh_credential_ids: string[]
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
  protocol_status: Record<string, { responsive: boolean; error?: string }>
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
  /** Discovery rules (phase 1): null when the rules never ran on this result. */
  disposition?: Disposition | null
  rule_plan?: RulePlan | null
  matched_rule_ids?: string[]
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

// ── Discovery rules (/discovery-v2/rules) ─────────────────────────────────────

export type RuleMode = 'draft' | 'active' | 'disabled'
export type AssetKind = 'device' | 'server'
export type GroupOp = 'all' | 'any' | 'none'
export type Decision = 'auto_import' | 'review' | 'ignore'
export type OperatorValueKind =
  | 'none'
  | 'text'
  | 'list'
  | 'number'
  | 'range'
  | 'cidr_list'
  | 'ip_range'
  | 'ref'
  | 'ref_list'
export type FieldType =
  | 'text'
  | 'oid'
  | 'version'
  | 'ip'
  | 'number'
  | 'set'
  | 'enum'
  | 'reference'
  | 'boolean'
export type RefSource = 'device_groups' | 'device_profiles' | 'snmp_credentials' | 'discovery_profiles'

export interface CatalogOperator {
  key: string
  label: string
  value: OperatorValueKind
}

export interface CatalogField {
  key: string
  type: FieldType
  label: string
  help: string
  operators: CatalogOperator[]
  values: string[] | null
  source: string | null
}

export interface CatalogAction {
  key: string
  label: string
  kind: 'single' | 'additive'
  value: 'decision' | 'ref' | 'text' | 'int' | 'tags'
  permission: string[]
  asset_kinds: AssetKind[]
  source: string | null
  help: string
  min: number | null
  max: number | null
}

export interface RuleCatalog {
  fields: CatalogField[]
  groups: { key: GroupOp; label: string }[]
  actions: CatalogAction[]
  decisions: Decision[]
  import_as: ImportTarget[]
  device_types: string[]
  limits: { max_depth: number; max_leaves: number; max_regex: number }
}

export interface ConditionLeaf {
  field: string
  operator: string
  value: any
}

export interface ConditionGroup {
  op: GroupOp
  conditions: ConditionNode[]
}

export type ConditionNode = ConditionGroup | ConditionLeaf

export interface RuleAction {
  key: string
  value: any
  import_as?: ImportTarget
}

export interface RuleEngineSettings {
  enabled: boolean
  max_auto_import_per_run: number
  license_reserve_nodes: number
  history_days: number
}

/** What the editor holds and sends (create / update / preview). */
export interface RuleDraft {
  name: string
  description: string | null
  mode: RuleMode
  asset_kinds: AssetKind[]
  profile_ids: string[] | null
  condition: ConditionGroup
  actions: RuleAction[]
  stop_processing: boolean
}

export interface DiscoveryRule extends RuleDraft {
  id: string
  priority: number
  triggers: string[]
  revision: number
  created_by: string | null
  updated_by: string | null
  activated_by: string | null
  activated_at: string | null
  created_at: string | null
  updated_at: string | null
  last_evaluated_at: string | null
  last_match_count: number | null
}

export interface RuleVersion {
  revision: number
  snapshot: Record<string, any>
  changed_by: string | null
  changed_by_name: string | null
  changed_at: string
  reason: string | null
}

export type Disposition =
  | 'imported'
  | 'ignored'
  | 'review'
  | 'would_import'
  | 'would_ignore'
  | 'existing'
  | 'no_match'
  | 'auto_import'
  | 'ignore'

export type PlanReason =
  | 'license_capacity_reached'
  | 'cap'
  | 'not_ready'
  | 'profile_review'
  | 'profile_ignore_only'
  | 'conflict'
  | 'error'

export interface PlanRef {
  rule_id: string
  rule_name: string | null
}

export interface PlanSetting extends PlanRef {
  value: any
  import_as?: ImportTarget
}

export interface PlanTrace extends PlanRef {
  matched: boolean
  /** Evaluated leaves as [field, operator, ok]; groups short-circuit. */
  leaves?: [string, string, boolean][]
  /** Why the rule did not take part (asset_kind, profile, trigger). */
  skipped?: string
}

export interface RulePlan {
  kind?: AssetKind
  matched: (PlanRef & { priority: number; revision?: number | null })[]
  settings: Record<string, PlanSetting>
  tags: (PlanRef & { value: string })[]
  shadowed: (PlanRef & { key: string; value: any; winner_rule_id: string; winner_rule_name: string | null })[]
  stopped_by: string | null
  trace: PlanTrace[]
  reason?: PlanReason | string | null
}

export interface PreviewRow {
  result_id: number | null
  ip_address?: string
  hostname?: string | null
  vendor?: string | null
  model?: string | null
  device_type?: string | null
  sys_object_id?: string | null
  status?: ResultStatus
  /** Only for a sample-facts preview. */
  facts?: Record<string, any>
  plan: RulePlan
  disposition: Disposition
  reason: PlanReason | string | null
}

export interface PreviewSummary {
  evaluated: number
  matched: number
  no_match?: number
  auto_import?: number
  ignore?: number
  would_import?: number
  would_ignore?: number
  review?: number
  existing?: number
}

export interface PreviewResponse {
  rows: PreviewRow[]
  summary: PreviewSummary
  import_mode: ImportMode
  /** null = unlimited */
  license_room: number | null
  max_auto_import_per_run: number
  discovery_run_id?: string
  profile_name?: string | null
}

export interface PreviewRequest {
  rule?: Partial<RuleDraft> & { id?: string; priority?: number }
  scope: 'with_active' | 'only'
  discovery_run_id?: string
  facts?: Record<string, any>
  import_mode?: ImportMode
  limit?: number
}

export interface EvaluateScanCounts {
  engine_run_id: string
  status: string
  evaluated: number
  matched: number
  imported: number
  ignored: number
  would_import: number
  would_ignore: number
  review: number
  existing: number
  no_match: number
  blocked_license: number
  blocked_cap: number
  errors: number
}

export type EngineRunOutcome =
  | 'applied'
  | 'simulated'
  | 'shadowed'
  | 'blocked_license'
  | 'blocked_cap'
  | 'not_ready'
  | 'error'

export interface EngineRun {
  id: string
  trigger: 'discovery' | 'manual'
  mode: 'apply' | 'annotate'
  status: string
  discovery_run_id: string | null
  profile_name: string | null
  started_at: string
  finished_at: string | null
  counts: Partial<EvaluateScanCounts> & Record<string, any>
}

export interface EngineRunItem {
  id: number
  ip_address: string | null
  discovery_result_id: number | null
  asset_id: string | null
  asset_kind?: string
  rule_id: string | null
  rule_name: string | null
  action_key: string | null
  before?: any
  after: any
  outcome: EngineRunOutcome | string
  detail: string | null
  created_at: string
}
