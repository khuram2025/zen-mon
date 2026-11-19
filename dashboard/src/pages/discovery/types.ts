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
/** child: a controller-managed FortiSwitch / FortiAP (phase 3), read through its FortiGate. */
export type AssetKind = 'device' | 'server' | 'child'
/**
 * When a rule runs by itself: on discovery scan results, on devices and servers as they are added, and (phase 3)
 * whenever an asset's facts change or the rule changes (re-checked once a minute), and (phase 4) over the whole
 * inventory at the scheduled times of the engine settings.
 */
export type RuleTrigger = 'discovery' | 'created' | 'changed' | 'scheduled'
/** When an asset stops matching: keep what the rule set, or revert it if it is still as the rule set it. */
export type OnUnmatch = 'keep' | 'revert'
/** set_once: a value edited by hand after the rule set it is kept; enforce: the rule puts its value back. */
export type Enforcement = 'set_once' | 'enforce'
/** Where a condition field is known: scan results, devices, servers. */
export type FieldSource = 'scan' | 'device' | 'server' | 'child'
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
  /** Names a server's agent reports (services, installed software, watched processes). */
  | 'inventory'
export type RefSource =
  | 'device_groups'
  | 'device_profiles'
  | 'snmp_credentials'
  | 'discovery_profiles'
  | 'manual_maps'
  | 'devices'
  | 'sensors'
  | 'agent_policies'
  | 'service_check_templates'

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
  value: 'decision' | 'ref' | 'text' | 'int' | 'tags' | 'on' | 'interfaces'
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
  limits: { max_depth: number; max_leaves: number; max_regex: number; max_interface_patterns?: number }
  modes?: { key: RuleMode; label: string }[]
  triggers?: { key: RuleTrigger; label: string }[]
  enforcement?: { key: Enforcement; label: string }[]
  on_unmatch?: { key: OnUnmatch; label: string }[]
  asset_kinds?: { key: AssetKind; label: string }[]
  /** Phase 4: what an interface pattern can test (text fields are case-insensitive regexes). */
  interface_fields?: { key: InterfaceField; label: string; value: 'regex' | 'types' | 'range' }[]
  interface_sets?: { key: InterfaceSet; label: string }[]
  /** Phase 4: the intervals a schedule may use (hours). */
  schedule_hours?: number[]
}

export type InterfaceField = 'if_name' | 'if_alias' | 'if_descr' | 'if_type' | 'speed'
export type InterfaceSet = 'monitor' | 'ignore'
/**
 * One pattern of the "Monitor or ignore interfaces" action. For each interface the first matching pattern (rules in
 * priority order, patterns in order) decides; interfaces nothing matches are left alone.
 */
export interface InterfacePattern {
  set: InterfaceSet
  field: InterfaceField
  /** regex (if_name / if_alias / if_descr), "6, 24" (if_type), "1000", "100-1000", "10000-", "-100" (speed, Mbit/s) */
  pattern: string
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
  /** An automatic re-check that would change more values than this waits for approval (phase 3). */
  max_changes_per_run: number
  /** Phase 4 match-growth stop: a rule changing more assets than max(min, pct % of its last match count) waits. 0 = off. */
  growth_breaker_pct?: number
  growth_breaker_min?: number
  /** Phase 4: rules with the "scheduled" trigger run over the inventory every N hours from HH:MM in this time zone. */
  schedule_enabled?: boolean
  schedule_every_hours?: number
  /** "HH:MM" */
  schedule_time?: string
  /** IANA name */
  schedule_timezone?: string
}

/** GET /schedule: when the scheduled rules run next and when they last ran. */
export interface RuleScheduleStatus {
  /** engine on and schedule on */
  enabled: boolean
  engine_enabled: boolean
  schedule_enabled: boolean
  /** ISO time of the next slot (computed even while the schedule is off) */
  next: string
  last_slot: string | null
  last_run_at: string | null
  /** active rules with the scheduled trigger */
  rules: number
  simulating: number
  last_run: { id: string; status: string; started_at: string; counts: Record<string, any> | null } | null
}

/** POST /schedule/run-now: applied, held for approval, or skipped. */
export interface ScheduleRunResult {
  skipped?: string
  engine_run_id?: string
  needs_approval?: string
  changes?: number
  reasons?: string[]
  assets?: number
  matched?: number
  applied?: number
  reverted?: number
  kept_manual_edit?: number
  not_reversible?: number
  skipped_changed?: number
  not_ready?: number
  next?: string
}

export interface UnmatchedRow {
  asset_kind: AssetKind
  asset_id: string
  name: string | null
  ip_address: string | null
  vendor: string | null
  model: string | null
  device_type: string | null
  os: string | null
  tags: string[]
}

/** GET /unmatched: devices, servers and children no active (optionally: no simulating) rule matches. */
export interface UnmatchedReport {
  kinds: Partial<Record<AssetKind, { assets: number; unmatched: number }>>
  assets: number
  unmatched: number
  rows: UnmatchedRow[]
  truncated: boolean
  rules: number
  /** the rows are limited to the viewer's tag scope (the counts are not) */
  scoped?: boolean
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
  on_unmatch: OnUnmatch
  /** Phase 4: automatic runs in which this rule would make more changes wait for approval; null = engine limit only. */
  max_changes_per_run?: number | null
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

export type EngineRunTrigger = 'discovery' | 'manual' | 'created' | 'changed' | 'scheduled' | 'undo'
export type EngineRunMode = 'apply' | 'annotate' | 'simulate'

export interface EngineRun {
  id: string
  trigger: EngineRunTrigger
  mode: EngineRunMode
  /** completed | partial | failed | running | undone | needs_approval | rejected */
  status: EngineRunStatus | string
  discovery_run_id: string | null
  profile_name: string | null
  started_at: string
  finished_at: string | null
  counts: Partial<EvaluateScanCounts> & Record<string, any>
  /** Why a run waits or was rejected ("120 changes exceed the limit of 100 …"). */
  detail?: string | null
  approved_by?: string | null
  approved_at?: string | null
}

export type EngineRunStatus = 'running' | 'completed' | 'partial' | 'failed' | 'undone' | 'needs_approval' | 'rejected'

export interface EngineRunItem {
  id: number
  ip_address: string | null
  discovery_result_id: number | null
  asset_id: string | null
  /** device | server | child | discovery_result */
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
  child_ids?: string[]
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

/** revert: the asset no longer matches a rule set to revert; `after` is the value restored (null = removed). */
export type ChangeOutcome = 'apply' | 'revert' | 'kept_manual_edit' | 'not_reversible'

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
  /** Values put back because an asset no longer matches a rule set to revert (phase 3). */
  reverts?: number
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
  reverted?: number
  /** Phase 4: sensor assignments waiting for the sensor to come online. */
  not_ready?: number
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

// ── Phase 3: approval of automatic re-checks, export / import, starter rules ──

/** GET /history/{id}/pending: the changes an automatic re-check holds until someone approves them. */
export interface PendingChanges {
  engine_run_id: string
  status: EngineRunStatus | string
  detail: string | null
  counts: Partial<InventorySummary> & Record<string, any>
  /** Assets the plan covers. */
  assets: number
  /** Phase 4: why the run waits (engine cap, a rule's own cap, the match-growth stop). */
  reasons?: string[]
  /** Phase 4: "changed" (automatic re-check) or "scheduled". */
  trigger?: EngineRunTrigger | string
  changes: InventoryChange[]
}

/** POST /history/{id}/approve or /reject. */
export interface ApprovalResult {
  engine_run_id: string
  status: string
  applied?: number
  reverted?: number
  kept_manual_edit?: number
  not_reversible?: number
  skipped_changed?: number
  not_ready?: number
}

/** GET /export: references by name ({"$ref": source, "name": ...}), never ids or secrets. */
export interface RulesExport {
  format: 'zenplus-discovery-rules'
  version: number
  exported_at: string
  rules: Record<string, any>[]
}

export interface RulesImportResult {
  created: { id: string; name: string; notes: string[] }[]
  skipped: { name: string; reason: string }[]
}

export interface StarterRule {
  key: string
  name: string
  description: string
  added: boolean
}

export interface StarterResult {
  created: { id: string; name: string; key: string }[]
}
