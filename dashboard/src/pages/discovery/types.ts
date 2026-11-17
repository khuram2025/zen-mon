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

export type RuleMode = 'draft' | 'simulate' | 'active' | 'disabled'
export type AssetKind = 'device' | 'server'
/** When a rule runs by itself: on discovery scan results and/or on devices and servers as they are added. */
export type RuleTrigger = 'discovery' | 'created'
/** set_once: a value edited by hand after the rule set it is kept; enforce: the rule puts its value back. */
export type Enforcement = 'set_once' | 'enforce'
/** Where a condition field is known: scan results, devices, servers. */
export type FieldSource = 'scan' | 'device' | 'server'
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
  /** Where the field has a value (phase 2); missing on an older API = scan results only. */
  applies_to?: FieldSource[]
}

export interface CatalogAction {
  key: string
  label: string
  kind: 'single' | 'additive'
  /** `on`: the only value is true ("Turn ping monitoring on"); a rule never turns monitoring off. */
  value: 'decision' | 'ref' | 'text' | 'int' | 'tags' | 'on'
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
  modes?: { key: RuleMode; label: string }[]
  triggers?: { key: RuleTrigger; label: string }[]
  enforcement?: { key: Enforcement; label: string }[]
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
  triggers: RuleTrigger[]
  enforcement: Enforcement
}

export interface DiscoveryRule extends RuleDraft {
  id: string
  priority: number
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
  | 'kept_manual_edit'
  | 'not_reversible'
  | 'skipped_changed'
  | 'reverted'
  | 'unchanged'
  | 'simulated'
  | 'shadowed'
  | 'blocked_license'
  | 'blocked_cap'
  | 'not_ready'
  | 'error'

export type EngineRunTrigger = 'discovery' | 'manual' | 'created' | 'undo'
export type EngineRunMode = 'apply' | 'annotate' | 'simulate'

export interface EngineRun {
  id: string
  trigger: EngineRunTrigger
  mode: EngineRunMode
  /** completed | partial | failed | running | undone */
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
  /** device | server | discovery_result */
  asset_kind?: string
  rule_id: string | null
  rule_name: string | null
  rule_revision?: number | null
  action_key: string | null
  item_key?: string | null
  before?: any
  after: any
  outcome: EngineRunOutcome | string
  detail: string | null
  created_at: string
}

// ── Phase 2: rules on inventory, undo, explain, conflicts ────────────────────

export interface InventoryTargets {
  kinds: AssetKind[]
  device_ids?: string[]
  server_ids?: string[]
  group_ids?: string[]
  tags?: string[]
  /** "10.0.0.0/16" */
  subnet?: string
  query?: string
}

export interface InventoryRunRequest {
  /** Omitted: every active rule, in order. Otherwise only these active rules. */
  rule_ids?: string[]
  targets: InventoryTargets
}

export type ChangeOutcome = 'apply' | 'kept_manual_edit' | 'not_reversible'

/** One value a rule would set on one device or server. */
export interface InventoryChange {
  asset_kind: AssetKind
  asset_id: string
  asset_name: string | null
  ip_address: string | null
  action_key: string
  /** "" for a single value, the lower-cased tag for a tag */
  item_key: string
  before: any
  after: any
  rule_id: string
  rule_name: string | null
  rule_revision: number | null
  outcome: ChangeOutcome
  detail: string | null
}

export interface InventorySummary {
  assets: number
  matched: number
  changes: number
  kept_manual_edit: number
  not_reversible: number
  assets_changed: number
  by_action: Record<string, number>
}

export interface InventoryPreview {
  changes: InventoryChange[]
  summary: InventorySummary
  digest: string
  truncated: boolean
}

export interface InventoryApplyResult extends Partial<InventorySummary> {
  engine_run_id: string
  status: string
  applied: number
  kept_manual_edit: number
  not_reversible: number
  skipped_changed: number
}

/** 409 detail of /inventory/apply when the inventory or the rules changed since the preview. */
export interface PreviewStaleDetail {
  code: 'preview_stale'
  message: string
  summary: InventorySummary
  digest: string
}

export interface UndoResult {
  engine_run_id: string
  undo_of: string
  status: string
  reverted: number
  skipped_changed: number
  not_reversible: number
  /** Devices a scan imported: listed for a deliberate delete, never deleted by undo. */
  imported_devices: { device_id: string; ip_address: string | null; hostname: string | null }[]
}

export interface AssetRuleSetting {
  action_key: string
  item_key: string | null
  /** what the rule set */
  value: any
  /** what the asset has now */
  current: any
  previous: any
  /** rule: still the rule's value; manual_edit: changed by hand since */
  state: 'rule' | 'manual_edit'
  rule_id: string | null
  rule_name: string | null
  run_id: string | null
  applied_at: string | null
}

export interface AssetAutomation {
  asset_kind: AssetKind
  asset_id: string
  name: string | null
  engine_enabled: boolean
  settings: AssetRuleSetting[]
  matching_rules: { rule_id: string; rule_name: string | null; priority: number }[]
  pending: InventoryChange[]
  history: {
    run_id: string
    rule_name: string | null
    action_key: string | null
    item_key: string | null
    before: any
    after: any
    outcome: string
    detail: string | null
    created_at: string
    trigger: EngineRunTrigger
  }[]
}

export interface ConflictRuleStat {
  rule_id: string
  rule_name: string
  priority: number
  matches: number
  wins: number
  shadowed: number
}

export interface ConflictReport {
  assets: number
  truncated: boolean
  rules: ConflictRuleStat[]
  overridden: {
    rule_id: string
    rule_name: string | null
    winner_rule_id: string
    winner_rule_name: string | null
    action_key: string
    assets: number
  }[]
  never_match: ConflictRuleStat[]
  never_win: ConflictRuleStat[]
}
