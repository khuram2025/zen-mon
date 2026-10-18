/**
 * Escalation matrix, on-call and notification grouping — API shapes
 * (work/ESCALATION-PLAN.md "API"). Every field a server might not send yet is
 * optional; the model normalizes raw responses before any screen reads them.
 */

export type Priority = 'P1' | 'P2' | 'P3' | 'P4'

export type TargetType = 'schedule' | 'user' | 'channel'

/** A step / fallback target. `name` is used when the server embeds it. */
export type Target = { type: TargetType; id: string; name?: string | null }

export type PolicyStep = {
  id?: string
  position: number
  delay_minutes: number
  name: string | null
  targets: Target[]
}

export type EscalationPolicy = {
  id: string
  name: string
  description: string | null
  enabled: boolean
  default_priorities: Priority[]
  ack_target_minutes: number | null
  repeat_minutes: number | null
  max_repeats: number
  review_minutes: number | null
  ack_expiry_minutes: number | null
  fallback_targets: Target[]
  steps: PolicyStep[]
  used_by_rules: number
  updated_at?: string | null
  /** The server breaks ties between policies claiming one priority by created_at (oldest first). */
  created_at?: string | null
}

/** Editor state: numbers as typed text so a half-typed field is not coerced. */
export type PolicyDraft = {
  name: string
  description: string
  enabled: boolean
  default_priorities: Priority[]
  ack_target_minutes: string
  repeat_minutes: string
  max_repeats: string
  review_minutes: string
  ack_expiry_minutes: string
  fallback_targets: Target[]
  steps: Array<{ key: string; delay_minutes: string; name: string; targets: Target[] }>
}

export type PreviewRecipient = {
  kind: string
  /** null for a channel (its name is in `via`); masked for roles without alerts.manage / users.manage. */
  address: string | null
  channel_id?: string | null
  user_id?: string | null
  user_name?: string | null
  via?: string | null
  notified_at?: string | null
}

export type PreviewStep = {
  position: number
  at_offset_minutes: number
  name: string | null
  recipients: PreviewRecipient[]
  gaps: string[]
  notified_at?: string | null
  /** The incident is at this step (route explanation). */
  current?: boolean
  /** The step reaches nobody, so the fallback route would be used. */
  uses_fallback?: boolean
  fallback_recipients?: PreviewRecipient[]
}

export type PolicyPreview = {
  steps: PreviewStep[]
  repeat?: { every_minutes?: number | null; max_repeats?: number | null } | null
  ack_expiry_minutes?: number | null
  fallback_gaps?: string[]
  review?: { every_minutes?: number | null; recipients?: PreviewRecipient[] | string; text?: string | null; continues_after_acknowledgement?: boolean } | string | null
  fallback?: PreviewRecipient[]
  warnings?: string[]
}

export type Restriction = { days: number[]; start: string; end: string }

export type OncallLayer = {
  id?: string
  name: string
  position: number
  shift_hours: number
  handoff_at: string
  members: string[]
  restrict_to: Restriction[]
}

export type OncallOverride = {
  id: string
  user_id: string
  user_name?: string | null
  starts_at: string
  ends_at: string
  reason?: string | null
}

export type OncallSchedule = {
  id: string
  name: string
  description: string | null
  timezone: string
  enabled: boolean
  layers: OncallLayer[]
  overrides?: OncallOverride[]
  /** Who is on call now (embedded by the list/detail responses). */
  now?: OnCallNow | null
  /** Names of the policies that target this schedule (deletion is refused while non-empty). */
  used_by?: string[]
}

export type ScheduleDraft = {
  name: string
  description: string
  timezone: string
  enabled: boolean
  layers: Array<{
    key: string
    id?: string
    name: string
    /** 'daily' | 'weekly' | 'custom' */
    rotation: RotationPreset
    shift_hours: string
    handoff_date: string
    handoff_time: string
    members: string[]
    restrict_to: Restriction[]
  }>
}

export type RotationPreset = 'daily' | 'weekly' | 'custom'

export type Shift = {
  start: string
  end: string
  user_id: string | null
  user_name: string | null
  layer: string | null
  override: boolean
}

export type OnCallNow = { users: Array<{ id: string; name: string }>; until: string | null }

export type CoverageEntry = { policy_id: string | null; policy_name: string | null; steps_ok: boolean; gaps: string[] }

export type NamedRef = { id: string | null; name: string; reason?: string | null }

export type Coverage = {
  priorities: Partial<Record<Priority, CoverageEntry>>
  schedules_without_members: NamedRef[]
  users_without_contacts: NamedRef[]
  unrouted_priorities: Priority[]
  schedule_gaps: Array<{ schedule_id: string | null; schedule_name: string; start: string; end: string }>
}

export type RouteSource = 'rule' | 'priority_default' | 'legacy_levels' | 'none'

export type RouteExplanation = {
  policy: { id: string | null; name: string | null; source: RouteSource } | null
  priority: Priority | null
  state: {
    step: number | null
    repeats: number
    acknowledged: boolean
    next_at: string | null
    next_action: string | null
  } | null
  steps: PreviewStep[]
  /** Server notes (why this route) and dry-run warnings. */
  warnings: string[]
  review?: PolicyPreview['review']
  repeat?: PolicyPreview['repeat']
  fallback?: PreviewRecipient[]
}

export type GroupBy = 'appliance' | 'site' | 'device_group' | 'scope_tag' | 'event_family'

export type GroupingSettings = {
  enabled: boolean
  group_by: GroupBy[]
  group_wait_seconds: number
  group_interval_seconds: number
  max_items: number
  /** 0 = no budget. */
  budget_per_recipient_per_hour: number
  critical_reserve: boolean
}

export type DigestStatus = 'open' | 'sending' | 'sent' | 'failed' | 'cancelled'

export type Digest = {
  id: string
  group_key: string
  group_label: string | null
  channel_name: string | null
  recipient: string | null
  status: DigestStatus | string
  reason: string
  release_condition: string
  opened_at: string | null
  release_at: string | null
  sent_at: string | null
  item_count: number
  error?: string | null
}

export type DigestItem = {
  id: string
  alert_id: string | null
  event: string
  severity: string | null
  summary: Record<string, unknown>
  added_at: string | null
  alert_status?: string | null
  message?: string | null
  already_recovered?: boolean
  asset?: string | null
  condition?: string | null
  value?: string | null
  incident_ref?: string | null
  priority?: string | null
}

export type DigestDetail = Digest & { items: DigestItem[] }

export type NotificationHold = {
  digest_id: string | null
  reason: string
  release_condition: string
  release_at: string | null
  status: string
}

export type ContactKind = 'email' | 'sms'

export type ContactMethod = {
  id?: string
  kind: ContactKind
  address: string
  label: string | null
  enabled: boolean
}

export type ResponseTargets = Record<Priority, number | null>
