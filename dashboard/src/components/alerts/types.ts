/**
 * Alert Center types — mirror docs/alerts-api-contract.md exactly.
 *
 * Workflow status (pending/active/acknowledged/resolved) and condition state
 * (still firing / suppressed / recovered) are separate concepts: see
 * `statusOf()` in AlertBits for how the UI derives the chip.
 */

export type AlertStatus = 'pending' | 'active' | 'acknowledged' | 'resolved'
export type AlertSeverity = 'critical' | 'warning' | 'info'
export type EntityKind = 'device' | 'server' | 'service_check' | 'sensor' | 'apm' | 'system'

export type UserRef = { id: string; name: string }

export type AlertEntity = {
  kind: EntityKind
  id: string | null
  name: string
  subtitle: string | null
  href: string | null
}

export type ClosureReason =
  | 'recovered' | 'manual' | 'suppressed' | 'superseded' | 'hold_cancelled' | 'dependency' | 'notice' | 'flapping'

export type AlertClosure = {
  reason: ClosureReason
  label: string
  by: UserRef | null
  note: string | null
}

export type AlertSuppression = {
  silence_id: string
  until: string | null
  forever: boolean
  reason: string | null
  by: UserRef | null
  created_at: string
}

export type AlertChannelRef = { id: string; name: string; type: string; enabled: boolean }

export type AlertDeliverySummary = {
  attempts: number
  accepted: number
  failed: number
  last_at: string | null
}

export interface AlertRow {
  id: string
  status: AlertStatus
  severity: AlertSeverity
  message: string
  triggered_at: string
  acknowledged_at: string | null
  resolved_at: string | null
  rule_id: string | null
  rule_name: string | null
  device_id: string | null
  server_id: string | null
  service_check_id: string | null
  sensor_id: string | null
  device_hostname: string | null
  device_ip: string | null
  server_hostname: string | null
  service_check_name: string | null
  sensor_name: string | null
  entity: AlertEntity
  acknowledged_by: UserRef | null
  assigned_to: UserRef | null
  assigned_at: string | null
  closure: AlertClosure | null
  is_notice: boolean
  /** 'flapping': a flapping started/stopped notice (metadata.flap_notice, flap_event 'start' | 'stop'). */
  notice_kind: 'recovery' | 'dependency' | 'hold_cancelled' | 'flapping' | null
  /** Pending rows only: held because the device is flapping ("Held: device flapping"), its class and the hold end. */
  flap_held?: boolean
  flap_kind?: 'reachability' | 'performance' | null
  hold_until?: string | null
  suppression: AlertSuppression | null
  snoozed: boolean
  snoozed_until: string | null
  ack_due_at: string | null
  ack_overdue: boolean
  channels: AlertChannelRef[]
  delivery: AlertDeliverySummary | null
  metadata: Record<string, unknown>
  /** P1..P4, stamped when the incident was raised (servers before 1.24.17 omit it). */
  priority?: string | null
}

export type AlertView = 'active' | 'suppressed' | 'pending' | 'acknowledged' | 'resolved' | 'open' | 'all'
export type AlertSort = 'newest' | 'oldest' | 'severity' | 'closed'

export type AlertListResponse = {
  data: AlertRow[]
  meta: {
    total: number
    skip: number
    limit: number
    sort: AlertSort
    view: AlertView | null
    server_time: string
    /** Snapshot anchor echoed back when the request pinned paging. */
    as_of?: string | null
    /** Matching rows that arrived after `as_of`. */
    newer?: number | null
  }
}

export type DurationStats = {
  count: number
  mean_s: number | null
  p50_s: number | null
  p95_s: number | null
}

export type AlertSummary = {
  server_time: string
  filtered: boolean
  filters: string[]
  open: {
    total: number
    active: number
    acknowledged: number
    suppressed: number
    pending: number
    /** Pending rows held because their device is flapping (part of `pending`; older servers omit it). */
    flap_held?: number
    by_severity: { critical: number; warning: number; info: number }
    unassigned_critical: number
    ack_overdue: number
    oldest_triggered_at: string | null
    aging: { lt_1h: number; h1_24: number; d1_7: number; gt_7d: number }
  }
  window: {
    from: string
    to: string
    basis: string
    opened: number
    closed: number
    by_reason: { recovered: number; manual: number; suppressed: number; superseded: number; administrative?: number }
    notices: number
    time_to_recover: DurationStats
    time_to_close: DurationStats
    time_to_ack: DurationStats
    clock_anomalies?: number
    delivery?: { attempts: number; accepted: number; failed: number; skipped: number; alerts_with_failures: number }
  }
  diagnostics: {
    open_rule_disabled: number
    open_rule_missing: number
    open_older_7d: number
    open_delivery_failed?: number
  }
  response_targets: { critical: number | null; warning: number | null; info: number | null }
}

export type AlertTrend = {
  server_time: string
  bucket: 'hour' | 'day'
  series: Array<{ t: string; opened: number; recovered: number; closed_other: number }>
  top_rules: Array<{ rule_id: string; name: string; count: number }>
  top_entities: Array<{ kind: EntityKind; id: string | null; name: string; count: number }>
}

export type RuleCondition = {
  metric: string
  operator: string
  threshold: number | null
  reset_threshold?: number | null
  [k: string]: unknown
}

export type RuleScopeKind =
  | 'device' | 'group' | 'tag' | 'location' | 'device_type' | 'service_check' | 'service_group'
  | 'server' | 'interface' | 'apm' | 'global'

export type AlertRuleDetail = {
  id: string
  name: string
  enabled: boolean
  severity: AlertSeverity | string
  metric: string
  operator: string
  threshold: number | null
  conditions: RuleCondition[] | null
  condition_logic: 'AND' | 'OR' | string | null
  condition_label: string | null
  trigger_on: 'any' | 'down' | 'degraded' | 'up' | null
  recovery_alert: boolean
  min_duration: number | null
  /** Device status rules: notify at once even while the device is flapping (older servers omit it). */
  notify_while_flapping?: boolean
  cooldown: number | null
  max_repeat: number | null
  schedule: { start: string | null; end: string | null; days: Array<string | number> } | null
  escalation_levels: Array<{
    after_minutes: number
    notify_channels: string[]
    channel_names: string[]
    repeat_every_minutes: number | null
  }>
  scope: { kind: RuleScopeKind | string; label: string; target: string | null } | null
  affected: { count: number | null; noun: string; shared: boolean } | null
  notify_channels: string[]
  editable: boolean
}

export type AlertEventKind =
  | 'triggered' | 'held' | 'confirmed' | 'acknowledged' | 'assigned' | 'unassigned' | 'comment'
  | 'snoozed' | 'unsnoozed' | 'suppression_expired' | 'escalated' | 'resolved' | 'recovered'
  | 'superseded' | 'hold_cancelled' | 'closed'

export type AlertEvent = {
  at: string
  kind: AlertEventKind | string
  label: string
  actor: UserRef | null
  source: 'user' | 'system' | 'evaluator'
  reason: string | null
  old_status: AlertStatus | null
  new_status: AlertStatus | null
  detail: Record<string, unknown> | null
}

export type AlertDelivery = {
  at: string
  channel_id: string | null
  channel_name: string | null
  channel_type: string | null
  kind: 'trigger' | 'recovery' | 'escalation' | 'all_clear' | 'reminder' | 'closure' | string
  outcome: 'accepted' | 'failed' | 'skipped'
  error: string | null
  latency_ms: number | null
}

export type RelatedAlert = AlertRow & {
  relation: 'same_entity' | 'same_rule_window' | 'dependency_parent' | string
  relation_label: string
}

export type AlertPermissions = {
  can_triage: boolean
  can_manage_rules: boolean
  can_suppress_forever: boolean
  can_edit_rule: boolean
}

export type AlertDetail = AlertRow & {
  server_time?: string
  permissions?: AlertPermissions
  rule: AlertRuleDetail | null
  policy_snapshot: Partial<AlertRuleDetail> | null
  escalation_state: { level: number; last_at: string | null; repeats: number; closed: boolean } | null
  evidence: Array<{ label: string; value: string | number | null }>
  recurrence: {
    same_condition_24h: number
    same_condition_7d: number
    flapping: boolean
    previous: Array<{ id: string; triggered_at: string; resolved_at: string | null; status: AlertStatus }>
  } | null
  related: RelatedAlert[]
  related_alerts?: AlertRow[]
  events: AlertEvent[]
  deliveries: AlertDelivery[]
  /** Notifications for this incident held in a digest (grouping / recipient budget), with why and when they go out. */
  notification_holds?: Array<{ digest_id: string | null; reason: string; release_condition: string; release_at: string | null; status: string }>
}

/** Every action returns the updated row plus whether anything changed. */
export type AlertActionResult = AlertRow & { changed: boolean }

export type Silence = {
  id: string
  entity: { kind: EntityKind; id: string | null; name: string }
  dedupe: string | null
  condition_label: string | null
  rule_name: string | null
  until: string | null
  forever: boolean
  reason: string | null
  created_by: UserRef | null
  created_at: string
  alert_id: string | null
  open_alerts: number
}

export type SnoozePreview = {
  condition_label: string | null
  entity: Partial<AlertEntity> | null
  rule: { id?: string | null; name?: string | null } | string | null
  open_alerts: number
  channels: AlertChannelRef[]
  existing: (Partial<Silence> & Partial<AlertSuppression>) | null
  can_forever: boolean
}

export type BulkAction = 'acknowledge' | 'resolve' | 'snooze' | 'assign'

export type BulkResponse = {
  results: Array<{ id: string; ok: boolean; changed: boolean; error: string | null }>
  summary: { ok: number; changed: number; failed: number }
}

export type Assignee = { id: string; name: string; username: string }

export type RuleImpact = {
  scope: { kind?: string; label?: string; target?: string | null } | string | null
  affected: { count: number | null; noun: string; shared: boolean } | null
  open_alerts: number
  enabled: boolean
}
