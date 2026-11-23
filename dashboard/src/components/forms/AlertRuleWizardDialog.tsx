/**
 * Alert rule wizard — properties, trigger, reset, scope, trigger actions,
 * escalation, reset actions, schedule, and review.
 */
import { DeviceTypeItems } from '@/components/forms/DeviceTypeItems'
import { FormEvent, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { INTERFACE_METRIC_KEYS, INTERFACE_ROLES, ROLE_LABEL } from '@/components/alerts/thresholdModel'
import {
  APM_MIN_WINDOW_S, DEFAULT_FLAP_WINDOW_S, NEW_PERCENT_CLEAR_S, RULE_SOURCES, conditionProblem,
  familyPayload, isRetired, metricSpec, metricsFor, newCondition, newPercentClearLevel, operatorsFor, ruleTimers,
  stateRuleInfo, templateIndex, timersText, triggerSummary, withNewPercentDefaults, badCodes, conditionsForStates,
  type RuleSource,
} from './alertRuleCatalog'
import { FamilyConditionFields, FamilyScopeFields, StatePicker, familyScopeProblem } from './AlertRuleFamilyFields'
import { eventExpiryFromApi, expiryWindowLabel } from '@/features/escalation/eventExpiry'
import { APM_EVENT_TRIGGERS, apmEventScope, apmScopeKind, isApmEvent } from './apmEventTriggers'
import {
  Bell,
  Check,
  CheckCircle2,
  ChevronRight,
  ChevronsUp,
  Clock,
  Copy,
  Eye,
  Loader2,
  Plus,
  RotateCcw,
  Settings2,
  Target,
  Trash2,
  Zap,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, copyText } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { Badge } from '@/components/ui/Badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { AlertWizardFooter, isExplicitWizardSave } from './AlertWizardFooter'
import { useTags, tagColor, tagColorMap } from '@/hooks/useTags'
import { NetflowConditionFields, NetflowDryRun, NetflowScopeFields } from './NetflowAlertFields'
import { ServerConditionTree } from './ServerConditionTree'
import {
  defaultNetflowForm,
  describeScopeDraft,
  formFromRule,
  isNetflowMetric,
  netflowPayload,
  summarizeNetflowForm,
  validateNetflowForm,
  type NetflowRuleForm,
} from './netflowAlertRule'
import {
  DEFAULT_SILENCE_SECONDS,
  MIN_SILENCE_SECONDS,
  SERVER_METRICS,
  downRuleCoversServers,
  serverAutoReset,
  serverMetric,
  serverOperators,
  serverDraftProblem,
  serverMetricKind,
  serverRuleCondition,
  serverRulePayload,
  serverScopeChange,
  serverScopeOf,
  serverScopeLabel,
  serverWatchChange,
  singleFromTree,
  treeFromDraft,
  SERVER_SCOPE_OPTIONS,
  type ServerScopeMode,
  type ServerTree,
} from './serverAlertRule'
import { TagPicker } from '@/components/tags/TagPicker'
import { groupsApi, treeOrder, type DeviceGroup } from '@/pages/groups/api'
import { usePolicies } from '@/features/escalation/api'
import {
  PRIORITIES, PRIORITY_META, autoPriorityText, derivePriority, disabledPolicyText, effectivePriority, fmtMinutes, legacyTiersText,
  policyForPriority, ruleEscalationSummary, stepSummary, tiersRemovedText,
} from '@/features/escalation/model'
import { PriorityBadge } from '@/features/escalation/ui'
import type { Priority } from '@/features/escalation/types'

type Cond = { metric: string; operator: string; threshold: number; reset_threshold?: number | null }

// One SLA escalation tier: if the alert is still active and unacknowledged
// after_minutes after triggering, page notify_channels. repeat_every_minutes
// re-pages the tier on that cadence until ack/resolve (null = no repeat).
type EscLevel = { after_minutes: number; notify_channels: string[]; repeat_every_minutes: number | null }

const ALL_OPS = ['>', '>=', '<', '<=', '==', '!='] as const

/** The server accepts at most this many legacy tiers (alert_rules.py). */
const MAX_ESC_LEVELS = 5

/**
 * How the rule escalates: the default policy for its priority, a named policy,
 * or the legacy inline tiers. Precedence on the server is the rule's enabled
 * policy > the rule's own tiers > the priority default, so "default" clears both
 * the policy and the tiers, and "policy" clears the tiers.
 */
type EscalationMode = 'default' | 'policy' | 'legacy'

type WizardState = {
  name: string
  description: string
  enabled: boolean
  severity: 'info' | 'warning' | 'critical'
  /** '' = derived from severity (critical -> P2, warning -> P3, info -> P4). */
  priority: '' | Priority
  escalation_mode: EscalationMode
  escalation_policy_id: string
  source: RuleSource
  conditions: Cond[]
  condition_logic: 'AND' | 'OR'
  trigger_on: 'any' | 'down' | 'up' | 'degraded'
  min_duration: number
  /** Metric (SNMP) rules: seconds the cleared condition must hold before the alert resolves. null = default (300 s). */
  clear_duration: number | null
  recovery_alert: boolean
  /** Device status rules: skip the automatic hold while the device is flapping (e.g. core routers). */
  notify_while_flapping: boolean
  reset_mode: 'auto' | 'none'
  device_id: string
  group_id: string
  device_type: string
  location: string
  scope_tag: string
  /** Server rules: one server, '' = all servers, '__pick__' = "one server" chosen but none picked yet. */
  server_id: string
  /** Server rules: all servers, one server, or the servers with a tag (scope_tag) and/or in a device group (group_id). */
  server_scope: 'all' | 'one' | 'tag' | 'group'
  service_check_id: string
  service_check_group_id: string
  trap_oid: string
  target: string
  /** SNMP device/interface/template metric rules: only devices on this template (or one customised from it). '' = any. */
  profile_id: string
  /** Interface metric rules: only interfaces with this role. '' = any. */
  if_role: string
  notify_channels: string[]
  cooldown: number
  max_repeat: number
  escalation_levels: EscLevel[]
  schedule_start: string
  schedule_end: string
  schedule_days: number[]
  email_subject: string
  email_body: string
  sms_template: string
  recovery_email_subject: string
  recovery_email_body: string
  recovery_sms_template: string
  /** NetFlow rules (source 'netflow'): condition, parameters and FlowFilter scope. */
  nf: NetflowRuleForm
  /** Server rules: two or more conditions joined by AND/OR (migrate-211). null = one condition. */
  condition_tree: ServerTree | null
}

const STEPS = [
  { id: 'properties', label: 'Properties', icon: Settings2 },
  { id: 'trigger', label: 'Trigger', icon: Zap },
  { id: 'reset', label: 'Reset', icon: RotateCcw },
  { id: 'scope', label: 'Scope', icon: Target },
  { id: 'actions', label: 'Trigger Actions', icon: Bell },
  { id: 'escalation', label: 'Escalation', icon: ChevronsUp },
  { id: 'reset_actions', label: 'Reset Actions', icon: CheckCircle2 },
  { id: 'schedule', label: 'Schedule', icon: Clock },
  { id: 'review', label: 'Review', icon: Eye },
] as const

type StepId = (typeof STEPS)[number]['id']

const DAY_LABELS = [
  { n: 1, label: 'Mon' }, { n: 2, label: 'Tue' }, { n: 3, label: 'Wed' },
  { n: 4, label: 'Thu' }, { n: 5, label: 'Fri' }, { n: 6, label: 'Sat' }, { n: 7, label: 'Sun' },
]

const CHANNEL_TYPE_LABEL: Record<string, string> = {
  email: 'Email', sms: 'SMS', webhook: 'Webhook', slack: 'Slack',
  telegram: 'Telegram', teams: 'Teams', discord: 'Discord', pagerduty: 'PagerDuty',
}

// Interface metrics take the interface filter and role (network_conditions.INTERFACE_METRICS). The device
// field list itself comes from the rule catalogue (alertRuleCatalog.METRICS), which also knows the retired keys.
const INTERFACE_METRICS = new Set<string>(INTERFACE_METRIC_KEYS)

// Server APM metrics are evaluated per service; browser RUM metrics are
// evaluated per application ID. Rates are fractions in 0–1.
const APM_METRICS = [
  ...APM_EVENT_TRIGGERS,
  { value: 'apm_latency_p95', label: 'p95 latency (ms)' },
  { value: 'apm_latency_p99', label: 'p99 latency (ms)' },
  { value: 'apm_latency_p50', label: 'p50 latency (ms)' },
  { value: 'apm_error_rate', label: 'Error rate (0.02 = 2%)' },
  { value: 'apm_throughput', label: 'Throughput (req/s)' },
  { value: 'apm_apdex', label: 'Apdex (0–1)' },
  { value: 'apm_rum_lcp_p75', label: 'Browser RUM — LCP p75 (ms)' },
  { value: 'apm_rum_inp_p75', label: 'Browser RUM — INP p75 (ms)' },
  { value: 'apm_rum_cls_p75', label: 'Browser RUM — CLS p75' },
  { value: 'apm_rum_error_session_rate', label: 'Browser RUM — error-affected sessions (0.05 = 5%)' },
  { value: 'apm_rum_resource_failure_rate', label: 'Browser RUM — failed resources (0.05 = 5%)' },
  { value: 'apm_rum_new_error_groups', label: 'Browser RUM — new error groups in window (count)' },
] as const

// Ordered by how often a template needs them. The phrasing variables at the
// top render finished English ("Interface utilisation on core-router-01 rose
// above 80%"); the raw ones below are the rule's stored values.
//
// Only the placeholders a save accepts for the rule's template kind are offered (GET
// /alert-rules/template-defaults `variables`, plus the family's own `extras`): a placeholder nothing fills
// is refused with 422 unknown_template_placeholder. This list only orders them.
const TEMPLATE_VARS = [
  '{event_sentence}', '{condition}', '{condition_sentence}', '{reading}',
  '{duration}', '{duration_sentence}', '{duration_suffix}',
  '{hostname}', '{ip_address}', '{host_suffix}', '{site}', '{site_suffix}', '{status}', '{severity}', '{rule_name}',
  '{group}', '{location}', '{device_type}', '{timestamp}',
  '{metric_label}', '{threshold_value}', '{metric}', '{operator}', '{threshold}',
  '{rtt}', '{packet_loss}',
  '{scenario_name}', '{failed_step}', '{error}', '{slo_name}',
]
/** Offline fallback when the server's list is not loaded: what every kind fills. */
const COMMON_VARS = ['rule_name', 'hostname', 'ip_address', 'severity', 'status', 'timestamp', 'site', 'group', 'site_suffix', 'event_sentence']

/** The placeholders to offer, in TEMPLATE_VARS order first, then the rest alphabetically. */
function orderedVars(valid: Iterable<string>): string[] {
  const set = new Set(valid)
  const head = TEMPLATE_VARS.filter((v) => set.has(v.slice(1, -1)))
  const rest = [...set].filter((v) => !TEMPLATE_VARS.includes(`{${v}}`)).sort().map((v) => `{${v}}`)
  return [...head, ...rest]
}

// The six template fields, in the order the editor shows them.
const TEMPLATE_FIELDS = [
  'email_subject', 'email_body', 'sms_template',
  'recovery_email_subject', 'recovery_email_body', 'recovery_sms_template',
] as const
type TemplateField = typeof TEMPLATE_FIELDS[number]
type TemplateSet = Partial<Record<TemplateField, string | null>>

// Offline fallback only. The live text comes from GET /alert-rules/template-
// defaults, which is also what the alert engine sends — a service-check or
// trap rule has different wording, and a copy kept here would drift from it.
const DEFAULTS: Record<TemplateField, string> = {
  email_subject: '[{severity}] {status}: {rule_name}',
  email_body: '{event_sentence}',
  sms_template: 'ZenPlus {severity} — {rule_name}: {event_sentence}',
  recovery_email_subject: '[{severity}] Resolved: {rule_name}',
  recovery_email_body: '{event_sentence}{duration_sentence}',
  recovery_sms_template: 'ZenPlus resolved — {rule_name}: {event_sentence}{duration_suffix}',
}

const DEFAULT_STATE: WizardState = {
  name: '',
  description: '',
  enabled: true,
  severity: 'critical',
  priority: '',
  escalation_mode: 'default',
  escalation_policy_id: '',
  source: 'device',
  conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }],
  condition_logic: 'AND',
  trigger_on: 'down',
  min_duration: 0,
  clear_duration: null,
  recovery_alert: true,
  notify_while_flapping: false,
  reset_mode: 'auto',
  device_id: '',
  group_id: '',
  device_type: '',
  location: '',
  scope_tag: '',
  server_id: '',
  server_scope: 'all',
  service_check_id: '',
  service_check_group_id: '',
  trap_oid: '',
  target: '',
  profile_id: '',
  if_role: '',
  notify_channels: [],
  cooldown: 300,
  max_repeat: 0,
  escalation_levels: [],
  schedule_start: '',
  schedule_end: '',
  schedule_days: [],
  // Blank means "use the built-in default", which the server writes in the
  // house style and keeps in step with the recovery/duration phrasing. Seeding
  // these with a field dump baked a copy of it onto every rule ever created,
  // and a stored template never picks up later improvements.
  email_subject: '',
  email_body: '',
  sms_template: '',
  recovery_email_subject: '',
  recovery_email_body: '',
  recovery_sms_template: '',
  nf: defaultNetflowForm(),
  condition_tree: null,
}

// Metrics a device rule reads from a confirmed status change; only those rules are held while a device flaps.
const STATUS_EVENT_METRICS = new Set(['ping_status', 'rtt', 'packet_loss'])

/** Default clear hold the evaluator applies when a metric rule leaves clear_duration empty. */
const DEFAULT_CLEAR_SECONDS = 300
/**
 * Reboot (and server restart/shutdown) alerts are events: they close on their own this long after the last
 * event when "Auto-clear after" is empty (network_alert_service.EVENT_HOLD_DEFAULT_S,
 * server_event_service.LIFECYCLE_HOLD_DEFAULT_S).
 */
const DEFAULT_EVENT_HOLD_SECONDS = 900
/** A reboot is detected from the device's uptime counter: an event, not a level with an operator or a reset value. */
const REBOOT_METRIC = 'uptime_reset'

/** A device rule whose only condition is "Reboot detected". */
function isRebootRule(s: WizardState): boolean {
  return s.source === 'device' && s.conditions.length === 1 && s.conditions[0]?.metric === REBOOT_METRIC
}

/** "15 min", "90 s": the event hold as the reset text reads it. */
function holdLabel(seconds: number | null): string {
  const v = seconds ?? DEFAULT_EVENT_HOLD_SECONDS
  if (v === 0) return 'at the next check'
  return v % 60 === 0 ? `${v / 60} min` : `${v} s`
}
// Percent metrics that clear 2 points on the healthy side of the trigger when no reset value is set.
const AUTO_CLEAR_METRICS = new Set(['cpu', 'memory'])
const AUTO_CLEAR_POINTS = 2

/** A device rule on polled SNMP metrics: periodic, with a breach hold and a clear hold. */
function usesMetricLifecycle(s: WizardState): boolean {
  return s.source === 'device' && s.conditions.every((c) => !STATUS_EVENT_METRICS.has(c.metric) && c.metric !== 'jitter')
}

/** The automatic clear level for a condition without a reset value, or null when none applies. */
function autoResetThreshold(c: Cond): number | null {
  if (c.reset_threshold != null || !AUTO_CLEAR_METRICS.has(c.metric)) return null
  const thr = Number(c.threshold)
  if (!Number.isFinite(thr)) return null
  if ((c.operator === '>' || c.operator === '>=') && thr - AUTO_CLEAR_POINTS >= 0) return thr - AUTO_CLEAR_POINTS
  if ((c.operator === '<' || c.operator === '<=') && thr + AUTO_CLEAR_POINTS <= 100) return thr + AUTO_CLEAR_POINTS
  return null
}

/** A device rule evaluated on confirmed status changes (ping status, RTT, packet loss). */
function isStatusRule(s: WizardState): boolean {
  return s.source === 'device' && s.conditions.some((c) => STATUS_EVENT_METRICS.has(c.metric))
}

/**
 * Whether the automatic flapping hold applies to the rule: a status rule whose trigger is not 'up'. The engine treats
 * an up-transition as a recovery, and recoveries are never held (they follow their trigger's fate).
 */
function flapHoldApplies(s: WizardState): boolean {
  if (!isStatusRule(s)) return false
  const trigger = s.conditions[0]?.metric === 'ping_status' ? s.trigger_on : 'any'
  return trigger !== 'up'
}

// The rules a template scope applies to (alert_rules._validate_template_scope): SNMP interface and device metrics
// and template metrics (tpl_<key>). An interface role applies when every condition is an interface metric.
const TEMPLATE_SCOPE_SCALARS = new Set(['cpu', 'memory', 'temperature', 'session_count', 'uptime_reset'])
function templateScopeApplies(conds: Cond[]): boolean {
  return conds.length > 0 && conds.every((c) => INTERFACE_METRICS.has(c.metric) || TEMPLATE_SCOPE_SCALARS.has(c.metric)
    || String(c.metric || '').startsWith('tpl_'))
}
function interfaceRoleApplies(conds: Cond[]): boolean {
  return conds.length > 0 && conds.every((c) => INTERFACE_METRICS.has(c.metric))
}

function ruleToState(rule: any): WizardState {
  const source =
    isNetflowMetric(rule.metric) ? 'netflow' : rule.metric === 'syslog' ? 'syslog' : rule.metric === 'trap'
      ? 'trap'
      : String(rule.metric || '').startsWith('apm_')
      ? 'apm'
      : String(rule.metric || '').startsWith('host_')
      ? 'server'
      // Self-contained like the rest (alertRuleCatalog.ruleSource is the same mapping).
      : String(rule.metric || '').startsWith('appliance_')
      ? 'appliance'
      : ['vm_host_disconnected', 'vm_datastore_pct', 'vm_alarm'].includes(rule.metric)
      ? 'virtualization'
      : ['snmp_unreachable', 'agent_no_metrics'].includes(rule.metric)
      ? 'gap'
      : String(rule.metric || '').startsWith('udt_')
      ? 'udt'
      : String(rule.metric || '').startsWith('netpath_')
      ? 'netpath'
      : rule.service_check_id || rule.service_check_group_id || ['service_status', 'service_tls_days', 'service_response_ms'].includes(rule.metric)
      ? 'service'
      : 'device'
  // Rules may store word operators (gt: server rules, and shipped device rules such as Packet Loss); the
  // editor's operator lists use symbols, so a stored "gt" no longer opens as an empty Operator field.
  const symbol = (op: any) => ({ gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=' } as Record<string, string>)[String(op ?? '').trim().toLowerCase()] || op
  const loaded: Cond[] =
    Array.isArray(rule.conditions) && rule.conditions.length
      ? rule.conditions.map((c: any) => ({
          metric: c.metric,
          operator: symbol(c.operator),
          threshold: c.threshold ?? 0,
          reset_threshold: c.reset_threshold ?? null,
        }))
      : [{ metric: rule.metric || 'ping_status', operator: symbol(rule.operator || '=='), threshold: rule.threshold ?? 0 }]
  return {
    ...DEFAULT_STATE,
    name: rule.name || '',
    description: rule.description || '',
    enabled: rule.enabled ?? true,
    severity: rule.severity || 'warning',
    // Self-contained (scripts/test-network-contracts.mjs evaluates this function on its own).
    priority: (['P1', 'P2', 'P3', 'P4'].includes(String(rule.priority || '').toUpperCase()) ? String(rule.priority).toUpperCase() : '') as WizardState['priority'],
    escalation_mode: rule.escalation_policy_id ? 'policy' : Array.isArray(rule.escalation_levels) && rule.escalation_levels.length ? 'legacy' : 'default',
    escalation_policy_id: rule.escalation_policy_id || '',
    source,
    // A server rule is one condition: the flat columns, plus the reset level its stored condition carries.
    conditions: source === 'server'
      ? [{
          metric: rule.metric,
          operator: symbol(rule.operator || 'gt'),
          threshold: rule.threshold ?? 0,
          reset_threshold: loaded.find((c) => c.metric === rule.metric)?.reset_threshold ?? null,
        }]
      : loaded,
    condition_logic: rule.condition_logic === 'OR' ? 'OR' : 'AND',
    trigger_on: rule.trigger_on || 'down',
    min_duration: rule.min_duration ?? rule.duration ?? 0,
    clear_duration: typeof rule.clear_duration === 'number' ? rule.clear_duration : null,
    recovery_alert: rule.recovery_alert ?? false,
    notify_while_flapping: rule.notify_while_flapping === true,
    reset_mode: rule.recovery_alert ? 'auto' : 'none',
    device_id: rule.device_id || '',
    group_id: rule.group_id || '',
    device_type: rule.device_type || '',
    location: rule.location || '',
    scope_tag: rule.scope_tag || '',
    server_id: rule.server_id || '',
    // Self-contained like the rest (see priority above): one server wins, then a group, then a tag.
    server_scope: rule.server_id ? 'one' : source === 'server' && rule.group_id ? 'group' : source === 'server' && rule.scope_tag ? 'tag' : 'all',
    service_check_id: rule.service_check_id || '',
    service_check_group_id: rule.service_check_group_id || '',
    trap_oid: rule.trap_oid || '',
    target: rule.target || '',
    profile_id: rule.profile_id || '',
    if_role: rule.if_role || '',
    notify_channels: Array.isArray(rule.notify_channels) ? rule.notify_channels : [],
    cooldown: rule.cooldown ?? 300,
    max_repeat: rule.max_repeat ?? 0,
    escalation_levels: Array.isArray(rule.escalation_levels)
      ? rule.escalation_levels.map((lv: any) => ({
          after_minutes: lv.after_minutes ?? 15,
          notify_channels: Array.isArray(lv.notify_channels) ? lv.notify_channels : [],
          repeat_every_minutes: lv.repeat_every_minutes ?? null,
        }))
      : [],
    schedule_start: rule.schedule_start || '',
    schedule_end: rule.schedule_end || '',
    schedule_days: Array.isArray(rule.schedule_days) ? rule.schedule_days : [],
    // The effective text — what this rule actually sends — not the stored
    // column, which is NULL on any rule using the built-in wording and left
    // the editor showing six empty boxes.
    ...templateState(rule.effective_templates || rule),
    nf: isNetflowMetric(rule.metric) ? formFromRule(rule) : defaultNetflowForm(),
    // A combined server rule (self-contained, like the fields above).
    condition_tree: source === 'server' && rule.condition_tree && Array.isArray(rule.condition_tree.items) ? rule.condition_tree : null,
  }
}

/** The six template fields as editor state: real text, never null. */
function templateState(source: TemplateSet | any): Record<TemplateField, string> {
  return TEMPLATE_FIELDS.reduce((acc, f) => {
    acc[f] = (source?.[f] as string) || ''
    return acc
  }, {} as Record<TemplateField, string>)
}

function stateToPayload(s: WizardState, defaults: TemplateSet = DEFAULTS) {
  const isService = s.source === 'service'
  const isTrap = s.source === 'trap'
  const isSyslog = s.source === 'syslog'
  const isDevice = s.source === 'device'
  const isApm = s.source === 'apm'
  const first = s.conditions[0]
  const conditions = isDevice && (s.conditions.length > 1 || first.reset_threshold != null) ? s.conditions : null
  const recovery = !isTrap && !isSyslog && s.reset_mode === 'auto'

  const payload: Record<string, any> = {
    name: s.name,
    description: s.description || null,
    enabled: s.enabled,
    metric: isService ? 'service_status' : isTrap ? 'trap' : isSyslog ? 'syslog' : first.metric,
    operator: isApm && isApmEvent(first.metric) ? '==' : isDevice || isApm || isSyslog ? first.operator : '==',
    threshold: isApm && isApmEvent(first.metric) ? 1 : isDevice || isApm || isSyslog ? first.threshold : 0,
    conditions,
    condition_logic: s.condition_logic,
    trigger_on: isService ? s.trigger_on : isDevice && first.metric === 'ping_status' ? s.trigger_on : 'any',
    recovery_alert: recovery,
    trap_oid: isTrap ? s.trap_oid.trim() || null : null,
    // target: interface filter for device interface metrics; APM service name
    // for APM rules (empty = every reporting service).
    target: isApm || isSyslog
      ? s.target.trim() || null
      : isDevice && s.conditions.some((c) => INTERFACE_METRICS.has(c.metric))
      ? s.target.trim() || null
      : null,
    // Template scope and interface role (migrate-193); null clears them, and they are refused on other rules.
    profile_id: isDevice && templateScopeApplies(s.conditions) ? s.profile_id || null : null,
    if_role: isDevice && interfaceRoleApplies(s.conditions) ? s.if_role || null : null,
    min_duration: isSyslog || isTrap || (isApm && isApmEvent(first.metric)) || isRebootRule(s) ? 0 : s.min_duration,
    clear_duration: usesMetricLifecycle(s) || isRebootRule(s) ? s.clear_duration : null,
    notify_while_flapping: flapHoldApplies(s) ? s.notify_while_flapping : false,
    severity: s.severity,
    priority: s.priority || null,
    escalation_policy_id: s.escalation_mode === 'policy' ? s.escalation_policy_id || null : null,
    cooldown: s.cooldown,
    max_repeat: s.max_repeat,
    device_id: isService || isApm ? null : s.device_id || null,
    group_id: isService || isApm ? null : s.group_id || null,
    device_type: isService || isApm ? null : s.device_type || null,
    location: isService || isApm ? null : s.location || null,
    scope_tag: isService || isApm ? null : s.scope_tag || null,
    service_check_id: isService ? s.service_check_id || null : null,
    service_check_group_id: isService ? s.service_check_group_id || null : null,
    notify_channels: s.notify_channels,
    escalation_levels: s.escalation_mode === 'legacy' && s.escalation_levels.length
      ? s.escalation_levels.map((lv) => ({
          after_minutes: lv.after_minutes,
          notify_channels: lv.notify_channels,
          repeat_every_minutes: lv.repeat_every_minutes || null,
        }))
      : null,
    schedule_start: s.schedule_start || null,
    schedule_end: s.schedule_end || null,
    schedule_days: s.schedule_days.length ? s.schedule_days : null,
    // Text still equal to the built-in default is stored as NULL, so the rule
    // keeps tracking the wording instead of freezing the copy the editor
    // displayed. (The server re-applies this too — see normalize_stored_template.)
    email_subject: customOrNull(s.email_subject, defaults.email_subject),
    email_body: customOrNull(s.email_body, defaults.email_body),
    sms_template: customOrNull(s.sms_template, defaults.sms_template),
    recovery_email_subject: recovery ? customOrNull(s.recovery_email_subject, defaults.recovery_email_subject) : null,
    recovery_email_body: recovery ? customOrNull(s.recovery_email_body, defaults.recovery_email_body) : null,
    recovery_sms_template: recovery ? customOrNull(s.recovery_sms_template, defaults.recovery_sms_template) : null,
  }
  if (s.source === 'netflow') {
    // One condition; the scope is the FlowFilter, not the device columns.
    const nf = netflowPayload(s.nf)
    Object.assign(payload, {
      metric: nf.metric, operator: nf.operator, threshold: nf.threshold,
      flow_scope: nf.flow_scope, flow_params: nf.flow_params,
      conditions: null, trigger_on: 'any', trap_oid: null, target: null, min_duration: 0,
      device_id: null, group_id: null, device_type: null, location: null, scope_tag: null,
      service_check_id: null, service_check_group_id: null,
    })
  }
  return payload
}

function customOrNull(value: string, fallback?: string | null): string | null {
  const text = (value || '').trim()
  if (!text) return null
  return text === (fallback || '').trim() ? null : value
}

const WIZARD_FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function AlertRuleWizardDialog({
  open,
  onOpenChange,
  rule,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  rule?: any
}) {
  const isEdit = !!rule?.id
  // A saved rule keeps its family: a server rule is saved through /host-alert-rules, every other kind through
  // /alert-rules, so switching between them would need a new rule rather than an edit.
  const editingServerRule = isEdit && String(rule?.metric || '').startsWith('host_')
  const qc = useQueryClient()
  const [step, setStep] = useState<StepId>('properties')
  const [s, setS] = useState<WizardState>(DEFAULT_STATE)
  const [scopeMode, setScopeMode] = useState<'all' | 'device' | 'group' | 'type' | 'location' | 'tag'>('all')
  const [preview, setPreview] = useState<any>(null)
  const [livePreview, setLivePreview] = useState<any>(null)
  const isRumMetric = s.source === 'apm' && s.conditions[0]?.metric.startsWith('apm_rum_')
  const apmEvent = s.source === 'apm' && isApmEvent(s.conditions[0]?.metric)
  const apmEventInfo = apmEventScope(s.conditions[0]?.metric)
  // A device rule evaluated on confirmed status changes (ping status, RTT, packet loss); unless it triggers on Up, its
  // alerts are held while the device flaps.
  const statusRule = isStatusRule(s)
  const flapHold = flapHoldApplies(s)
  const isServer = s.source === 'server'
  // A combined server rule takes the threshold rules' holds and clear settings.
  const serverKind = isServer ? (s.condition_tree ? 'pct' : serverMetricKind(s.conditions[0]?.metric)) : null
  const isLifecycle = isServer && serverKind === 'event'
  const rebootRule = isRebootRule(s)
  const first0 = s.conditions[0]
  // Interface speed change: an event like a reboot (no breach hold, auto-clear). Flapping: the hold is the window.
  const speedRule = s.source === 'device' && s.conditions.length === 1 && first0?.metric === 'if_speed_changed'
  const flapRule = s.source === 'device' && s.conditions.some((c) => c.metric === 'if_flaps')
  const eventAutoClear = rebootRule || speedRule
  // Families with one condition and their own inputs (alertRuleCatalog / AlertRuleFamilyFields).
  const familySource = ['gap', 'appliance', 'virtualization', 'udt', 'netpath'].includes(s.source)
    || (s.source === 'service' && !!first0 && first0.metric !== 'service_status')
  // Which timer fields this rule's evaluator honours; the others are hidden or explained.
  const timers = ruleTimers({ metric: first0?.metric, conditions: s.source === 'device' ? s.conditions : null })
  // Events that never recover on their own: traps, syslog, Device Tracker events (they expire) and route changes.
  const eventOnly = s.source === 'trap' || s.source === 'syslog' || (familySource && (timers.closes === 'expires' || timers.closes === 'none'))
  const nfErrors = useMemo(() => (s.source === 'netflow' ? validateNetflowForm(s.nf) : {}), [s.source, s.nf])

  // Which default wording this rule's notifications are built from. Service
  // checks and traps lead with different sentences than a device does, so the
  // editor has to show the set that will actually be sent.
  // Mirrors alert_phrasing.template_kind: a service-check metric rule not bound to a check uses the device set.
  const templateKind = s.source === 'syslog' ? 'syslog' : s.source === 'trap' ? 'trap'
    : s.source === 'service' && (first0?.metric === 'service_status' || !!s.service_check_id || !!s.service_check_group_id) ? 'service'
    : 'device'
  const { data: templateDefaultsMap } = useQuery<any>({
    queryKey: ['alert-rules', 'template-defaults'],
    queryFn: async () => (await api.get('/alert-rules/template-defaults')).data,
    staleTime: Infinity,
  })
  const tplDefaults: TemplateSet = templateDefaultsMap?.[templateKind] || DEFAULTS
  // The placeholders a save accepts for this kind, plus the ones this family's sender adds (APM events,
  // appliance, NetFlow). Offline: the saved rule's own list, else what every kind fills.
  const templateVars = useMemo(() => {
    const kindVars: string[] | undefined = templateDefaultsMap?.variables?.[templateKind]
    const extras: string[] = templateDefaultsMap?.extras?.[first0?.metric || ''] || []
    if (kindVars) return orderedVars([...kindVars, ...extras])
    if (Array.isArray(rule?.template_variables) && rule?.template_kind === templateKind) return orderedVars(rule.template_variables)
    return orderedVars(COMMON_VARS)
  }, [templateDefaultsMap, templateKind, first0?.metric, rule])
  const hasResetTemplates = tplDefaults.recovery_email_subject != null

  // Re-seed the message fields when the rule kind changes — but only the ones
  // still holding a default, so switching source never discards text the
  // operator wrote. `appliedDefaults` is the set the current text was seeded
  // from, which is what makes "did they edit this?" answerable.
  const appliedDefaults = useRef<TemplateSet>(DEFAULTS)
  const tplDefaultsKey = JSON.stringify(tplDefaults)
  useEffect(() => {
    if (!open) return
    const prev = appliedDefaults.current
    setS((st) => {
      const next = { ...st }
      for (const f of TEMPLATE_FIELDS) {
        const current = (st[f] || '').trim()
        if (!current || current === ((prev?.[f] as string) || '').trim()) {
          next[f] = (tplDefaults[f] as string) || ''
        }
      }
      return next
    })
    appliedDefaults.current = tplDefaults
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, templateKind, tplDefaultsKey])

  const stepIdx = STEPS.findIndex((st) => st.id === step)

  const { data: devicesResp } = useQuery<any>({
    queryKey: ['devices', 'list-min'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    enabled: open,
  })
  const devices = devicesResp?.data || []

  const { data: groups } = useQuery<any[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })

  // Device templates: the template metric picker and the "only devices on template" scope.
  const { data: templatesResp } = useQuery<any>({
    queryKey: ['snmp-profiles'],
    queryFn: async () => (await api.get('/snmp/profiles')).data,
    enabled: open && s.source === 'device',
    staleTime: 60_000,
  })
  const templates: WizardTemplate[] = Array.isArray(templatesResp) ? templatesResp : templatesResp?.data || []
  const tplNames = useMemo(() => templateMetricNames(templates), [templates])
  // Template names, units and state (enum) value maps: the state picker and the summaries.
  const tplIndex = useMemo(() => templateIndex(templates), [templates])
  const stateInfo = s.source === 'device' ? stateRuleInfo(s.conditions, tplIndex) : null
  // Event alerts (trap, syslog, Device Tracker events) close by themselves after this window.
  const { data: expiryRaw } = useQuery<any>({
    queryKey: ['alerts', 'event-expiry'],
    queryFn: async () => (await api.get('/alerts/event-expiry')).data,
    enabled: open && (s.source === 'trap' || s.source === 'syslog' || s.source === 'udt'),
    staleTime: 60_000,
    retry: false,
  })
  const expiry = eventExpiryFromApi(expiryRaw)
  const expiryText = expiry.enabled
    ? `Event alerts close by themselves once ${expiryWindowLabel(expiry.hours)} pass without a new event (Escalation & On-call → Alert storms → Event alert expiry); closing sends no notification. A repeat while one is open updates it instead of adding another.`
    : 'Event alert expiry is off (Escalation & On-call → Alert storms), so these alerts stay open until someone resolves them. A repeat while one is open updates it instead of adding another.'

  const { data: locations = [] } = useQuery<string[]>({
    queryKey: ['devices', 'locations'],
    queryFn: async () => (await api.get('/devices/locations')).data,
    enabled: open,
  })

  const { data: tags = [] } = useTags(open)
  const tagColors = useMemo(() => tagColorMap(tags), [tags])

  const { data: serviceChecksResp } = useQuery<any>({
    queryKey: ['service-checks', 'list-min'],
    queryFn: async () => (await api.get('/service-checks?limit=200')).data,
    enabled: open,
  })
  const serviceChecks = serviceChecksResp?.data || []

  const { data: serversResp } = useQuery<any>({
    queryKey: ['servers', 'list-min'],
    queryFn: async () => (await api.get('/servers?page_size=200&sort=display_name')).data,
    enabled: open && isServer,
  })
  const servers: any[] = serversResp?.items || []
  const serverScope = isServer ? serverScopeOf(s) : 'all'

  // Server rules scoped to a device group: every group (static and dynamic), in tree order.
  const { data: deviceGroupList } = useQuery<DeviceGroup[]>({
    queryKey: ['groups-tree'],
    queryFn: groupsApi.list,
    enabled: open && (isServer || s.source === 'gap'),
    retry: false,
    staleTime: 60_000,
  })
  const deviceGroups = useMemo(() => treeOrder(deviceGroupList || []), [deviceGroupList])

  const { data: serviceGroups = [] } = useQuery<any[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    enabled: open,
  })

  const policiesQ = usePolicies(open)
  const policies = policiesQ.data ?? []
  const effPriority = effectivePriority(s.priority, s.severity)
  const defaultPolicy = policyForPriority(policies, effPriority)
  const chosenPolicy = policies.find((p) => p.id === s.escalation_policy_id) ?? null

  const { data: channelsResp } = useQuery<any>({
    queryKey: ['channels', 'list-min'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })
  const channels: any[] = Array.isArray(channelsResp) ? channelsResp : channelsResp?.data || []

  const { data: apmServicesResp } = useQuery<any>({
    queryKey: ['apm', 'services', 'list-min'],
    queryFn: async () => (await api.get('/apm/services?range=24h')).data,
    enabled: open && s.source === 'apm',
  })
  const apmServices: string[] = (apmServicesResp?.services || []).map((x: any) => x.name)
  const { data: apmEntitiesResp } = useQuery<any>({
    queryKey: ['apm', 'alert-event-scope', s.conditions[0]?.metric],
    queryFn: async () => (await api.get(s.conditions[0]?.metric === 'apm_synthetic_down' ? '/apm/synthetics' : '/apm/slos')).data,
    enabled: open && apmEvent,
  })
  const apmEntities: { id: string; name: string }[] = apmEntitiesResp?.monitors ?? apmEntitiesResp?.items ?? []
  const { data: rumScopeRows } = useQuery<any>({
    queryKey: ['apm', 'rum', 'views', 'alert-scope'],
    queryFn: async () => (await api.get('/apm/rum/views?range=24h&page=1&page_size=100&sort=last_seen&order=desc')).data,
    enabled: open && isRumMetric,
  })
  const rumApplications: string[] = Array.from(new Set(
    (rumScopeRows?.items || []).map((item: any) => `${item.application_id} @ ${item.env || 'unknown'}`),
  ))

  useLayoutEffect(() => {
    if (!open) return
    setStep('properties')
    setPreview(null)
    if (rule) {
      const st = ruleToState(rule)
      setS(st)
      // The loaded text is this rule's own defaults resolved server-side, so
      // that is the set to judge later edits against.
      appliedDefaults.current = rule.default_templates || DEFAULTS
      setScopeMode(
        st.group_id ? 'group' : st.device_type ? 'type' : st.location ? 'location'
          : st.scope_tag ? 'tag' : st.device_id ? 'device' : 'all',
      )
    } else {
      setS({ ...DEFAULT_STATE, ...templateState(tplDefaults) })
      appliedDefaults.current = tplDefaults
      setScopeMode('all')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rule])

  /**
   * The /alert-rules body: the generic builder, then the family's own fields (alertRuleCatalog.familyPayload),
   * and for a new rule the percent defaults (clear 5 points below the limit after 900 s, plan step 2.2).
   */
  const buildPayload = (st: WizardState) => {
    const draft = !isEdit && st.source === 'device' ? { ...st, ...withNewPercentDefaults(st.conditions, st.clear_duration) } : st
    return familyPayload(draft, stateToPayload(draft, tplDefaults))
  }

  const save = useMutation({
    mutationFn: async ({ payload, server }: { payload: any; server: boolean }) => {
      const base = server ? '/host-alert-rules' : '/alert-rules'
      if (isEdit) return (await api.put(`${base}/${rule.id}`, payload)).data
      return (await api.post(base, payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Alert rule updated' : 'Alert rule created')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const previewMut = useMutation({
    // Rendered from the draft itself — no id needed, nothing written. This
    // used to create a disabled throwaway rule and delete it again for every
    // preview, which leaked a rule into the list whenever the delete failed.
    mutationFn: async () =>
      (await api.post('/alert-rules/preview', buildPayload(s))).data,
    onSuccess: (data) => setPreview(data),
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const simulateMut = useMutation({
    mutationFn: async () => (await api.post(`/alert-rules/${rule.id}/simulate`)).data,
    onSuccess: (data: any) => {
      const details = (data.results || []).map((r: any) => `${r.channel}: ${r.status}`).join(', ')
      toast.success(data.message || 'Test sent', details)
    },
    onError: (e: any) => toast.error('Simulate failed', apiErrorMessage(e)),
  })

  const updateCond = (i: number, patch: Partial<Cond>) =>
    setS((st) => ({ ...st, conditions: st.conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c)) }))
  const addCond = () =>
    setS((st) => ({ ...st, conditions: [...st.conditions, { metric: 'rtt', operator: '>', threshold: 100 }] }))
  const removeCond = (i: number) =>
    setS((st) => ({
      ...st,
      conditions: st.conditions.length > 1 ? st.conditions.filter((_, idx) => idx !== i) : st.conditions,
    }))
  /**
   * A template metric picked for condition i. A state (enum) metric becomes the whole rule: its bad states
   * (template_states.bad_codes, else the first non-healthy one) as AND-combined conditions, a 2-minute hold on
   * a new rule. A gauge starts at the template's critical level.
   */
  const pickTemplateMetric = (i: number, m: WizardTemplateMetric, templateId: string) => {
    const info = m.type === 'enum' ? tplIndex.states[m.key] : undefined
    if (info) {
      const metric = `tpl_${m.key}`
      const bad = badCodes(info)
      const codes = Object.keys(info.labels).map(Number).sort((a, b) => a - b)
      const start = bad.length ? bad : codes.filter((c) => info.labels[c][1] !== 'ok').slice(0, 1)
      const conds = conditionsForStates(metric, info, start.length ? start : codes.slice(0, 1))
      if (!conds) return
      setS((st) => ({ ...st, profile_id: st.profile_id || templateId, condition_logic: 'AND', target: '', if_role: '',
        min_duration: isEdit || st.min_duration ? st.min_duration : 120,
        conditions: conds.map((c) => ({ ...c, reset_threshold: null })) }))
      return
    }
    setS((st) => ({
      ...st,
      // A template metric defaults its scope to the chosen template.
      profile_id: st.profile_id || templateId,
      conditions: (stateRuleInfo(st.conditions, tplIndex) ? [st.conditions[0]] : st.conditions).map((x, idx) => (idx !== i ? x : {
        ...x,
        metric: `tpl_${m.key}`,
        operator: m.thresholds?.op === '<=' ? '<=' : ['>', '>='].includes(x.operator) ? x.operator : '>=',
        threshold: m.thresholds?.crit ?? m.thresholds?.warn ?? x.threshold,
        reset_threshold: null,
      })),
    }))
  }
  const toggleChannel = (id: string) =>
    setS((st) => ({
      ...st,
      notify_channels: st.notify_channels.includes(id)
        ? st.notify_channels.filter((c) => c !== id)
        : [...st.notify_channels, id],
    }))
  const updateEsc = (i: number, patch: Partial<EscLevel>) =>
    setS((st) => ({
      ...st,
      escalation_levels: st.escalation_levels.map((lv, idx) => (idx === i ? { ...lv, ...patch } : lv)),
    }))
  const toggleEscChannel = (i: number, id: string) =>
    setS((st) => ({
      ...st,
      escalation_levels: st.escalation_levels.map((lv, idx) =>
        idx === i
          ? {
              ...lv,
              notify_channels: lv.notify_channels.includes(id)
                ? lv.notify_channels.filter((c) => c !== id)
                : [...lv.notify_channels, id],
            }
          : lv,
      ),
    }))
  const addEscLevel = () =>
    setS((st) => {
      const last = st.escalation_levels[st.escalation_levels.length - 1]
      return {
        ...st,
        escalation_levels: [
          ...st.escalation_levels,
          { after_minutes: last ? last.after_minutes * 2 : 15, notify_channels: [], repeat_every_minutes: null },
        ],
      }
    })
  const removeEscLevel = (i: number) =>
    setS((st) => ({ ...st, escalation_levels: st.escalation_levels.filter((_, idx) => idx !== i) }))
  const toggleDay = (n: number) =>
    setS((st) => ({
      ...st,
      schedule_days: st.schedule_days.includes(n)
        ? st.schedule_days.filter((d) => d !== n)
        : [...st.schedule_days, n].sort((a, b) => a - b),
    }))

  const setTemplate = (field: TemplateField, value: string) =>
    setS((st) => ({ ...st, [field]: value }))

  // Live rendered mail on the two steps where the wording is written. The
  // template field is one sentence inside a branded HTML layout — without
  // seeing the layout around it, "{event_sentence}" tells an operator nothing
  // about the mail that lands in their inbox.
  const onMessageStep = step === 'actions' || step === 'reset_actions'
  const draftPayload = useMemo(
    () => JSON.stringify(buildPayload(s)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [s, tplDefaultsKey],
  )
  useEffect(() => {
    // Server rules send the built-in wording, so their message steps have no templates to render.
    if (!open || !onMessageStep || isServer) return
    const t = setTimeout(() => {
      api.post('/alert-rules/preview', JSON.parse(draftPayload))
        .then((r) => setLivePreview(r.data))
        .catch(() => {})
    }, 400)
    return () => clearTimeout(t)
  }, [open, onMessageStep, draftPayload, isServer])

  const copyTriggerToReset = () => {
    setS((st) => ({
      ...st,
      recovery_email_subject: `RESOLVED: ${st.email_subject}`,
      recovery_email_body: st.email_body,
      recovery_sms_template: st.sms_template,
    }))
    toast.success('Trigger templates copied to reset actions')
  }

  const condMetricName = (m: string) => (m.startsWith('tpl_') ? `${tplNames[m.slice(4)] || m.slice(4)} (template)` : m)
  const summary = useMemo(() => {
    const first = s.conditions[0]
    const condText =
      s.source === 'netflow'
        ? summarizeNetflowForm(s.nf)
        : s.source === 'server'
        ? serverRuleCondition({ ...first, target: s.target, min_duration: s.min_duration, condition_tree: s.condition_tree })
        : s.source === 'trap'
        ? s.trap_oid ? `Trap OID prefix ${s.trap_oid}` : 'Any SNMP trap'
        : s.source === 'service'
        ? `Service status → ${s.trigger_on}`
        : s.source === 'apm' && isApmEvent(first.metric)
        ? APM_EVENT_TRIGGERS.find(x => x.value === first.metric)!.label
        : s.source === 'apm'
        ? s.conditions.length > 1
          ? s.conditions.map((c) => `${condMetricName(c.metric)} ${c.operator} ${c.threshold}`).join(` ${s.condition_logic} `)
          : `${condMetricName(first.metric)} ${first.operator} ${first.threshold}`
        // Every other family in words, with its unit ("Data disk used > 85%", "IPsec Tunnels: Status is Down").
        : triggerSummary({ ...buildPayload(s), conditions: s.conditions }, tplIndex).detail
    return { condText }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s, tplNames, tplIndex])

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!isExplicitWizardSave(stepIdx, STEPS.length, save.isPending, (e.nativeEvent as SubmitEvent).submitter)) return
    if (!s.name.trim()) {
      toast.error('Name required')
      setStep('properties')
      return
    }
    // A scope mode was chosen but nothing selected — saving now would silently
    // apply the rule to every device, which is never what was meant.
    const scopeEmpty =
      (scopeMode === 'device' && !s.device_id) ||
      (scopeMode === 'group' && !s.group_id) ||
      (scopeMode === 'type' && !s.device_type) ||
      (scopeMode === 'location' && !s.location) ||
      (scopeMode === 'tag' && !s.scope_tag)
    if (s.source === 'netflow') {
      const errs = validateNetflowForm(s.nf) as Record<string, string>
      const keys = Object.keys(errs)
      if (keys.length) {
        const scopeKeys = new Set(['scope', 'exporter', 'iface', 'iface_dir', 'talker', 'src', 'dst', 'not_talker',
          'not_src', 'not_dst', 'protocol', 'port', 'src_port', 'dst_port', 'dst_ports', 'service_port', 'app',
          'dscp', 'netclass', 'tcpflag', 'hour', 'dow', 'group', 'asn', 'ip_version', 'direction', 'min_bytes',
          'max_bytes', 'count'])
        toast.error('NetFlow rule incomplete', errs[keys[0]])
        setStep(keys.some((k) => scopeKeys.has(k)) && keys.every((k) => scopeKeys.has(k)) ? 'scope' : 'trigger')
        return
      }
    }
    if (s.source === 'device' && s.conditions.some((c) => c.metric === 'tpl_')) {
      toast.error('Choose a template metric', 'Pick the template and the metric to watch, or another field.')
      setStep('trigger')
      return
    }
    // The server's validators for the condition (single-condition metrics, operators, limits, clear levels,
    // retired keys) and for the family scopes, so a refusal is caught on the step that can fix it.
    if (s.source !== 'server' && s.source !== 'netflow' && s.source !== 'trap' && s.source !== 'syslog' && s.source !== 'apm'
      && !(s.source === 'service' && first0?.metric === 'service_status')) {
      const kept = isEdit ? [rule?.metric, ...(Array.isArray(rule?.conditions) ? rule.conditions.map((c: any) => c?.metric) : [])] : []
      const problem = conditionProblem(s.conditions, kept.filter(Boolean))
      if (problem) {
        toast.error('Trigger incomplete', problem)
        setStep('trigger')
        return
      }
      const scopeProblem = familyScopeProblem(s)
      if (scopeProblem) {
        toast.error('Scope incomplete', scopeProblem)
        setStep('scope')
        return
      }
    }
    if (s.source === 'server') {
      const problem = serverDraftProblem(s)
      if (problem) {
        toast.error('Server rule incomplete', problem.message)
        setStep(problem.step)
        return
      }
    }
    if ((['device', 'trap', 'syslog', 'udt'].includes(s.source) || (s.source === 'gap' && first0?.metric === 'snmp_unreachable')) && scopeEmpty) {
      toast.error('Scope incomplete', `Select a ${scopeMode === 'type' ? 'device type' : scopeMode}, or choose "All devices".`)
      setStep('scope')
      return
    }
    if (s.escalation_mode === 'policy' && !s.escalation_policy_id) {
      toast.error('Choose an escalation policy', 'Or use the priority default.')
      setStep('escalation')
      return
    }
    const legacyLevels = s.escalation_mode === 'legacy' && !isServer ? s.escalation_levels : []
    if (legacyLevels.length > MAX_ESC_LEVELS) {
      toast.error(`At most ${MAX_ESC_LEVELS} escalation levels`)
      setStep('escalation')
      return
    }
    for (let i = 0; i < legacyLevels.length; i++) {
      const lv = legacyLevels[i]
      if (!lv.notify_channels.length) {
        toast.error(`Escalation level ${i + 1} has no channels`, 'Pick at least one channel or remove the level.')
        setStep('escalation')
        return
      }
      if (!(lv.after_minutes >= 1)) {
        toast.error(`Escalation level ${i + 1} needs a delay of at least 1 minute`)
        setStep('escalation')
        return
      }
      if (i > 0 && lv.after_minutes <= legacyLevels[i - 1].after_minutes) {
        toast.error('Escalation delays must increase', `Level ${i + 1} must fire later than level ${i}.`)
        setStep('escalation')
        return
      }
    }
    save.mutate(isServer
      ? { payload: serverRulePayload(isEdit ? s : { ...s, ...withNewPercentDefaults(s.conditions, s.clear_duration) }), server: true }
      : { payload: buildPayload(s), server: false })
  }
  const isService = s.source === 'service'

  // The drawer is a modal dialog: named by its title, focus moves into it on
  // open, Tab stays inside, Escape closes it and focus returns to the opener.
  const titleId = useId()
  const descId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const active = document.activeElement
    const opener = active instanceof HTMLElement && active !== document.body ? active : null
    panelRef.current?.focus({ preventScroll: true })
    return () => {
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [open])
  const onPanelKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const panel = panelRef.current
    // Keys from a popover rendered in a portal (a Select list) are its own.
    if (!panel || !panel.contains(e.target as Node)) return
    if (e.key === 'Escape') {
      e.stopPropagation()
      e.preventDefault()
      if (!save.isPending) onOpenChange(false)
      return
    }
    if (e.key !== 'Tab') return
    const items = Array.from(panel.querySelectorAll<HTMLElement>(WIZARD_FOCUSABLE)).filter((el) => el.offsetParent !== null)
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" aria-hidden onClick={() => { if (!save.isPending) onOpenChange(false) }} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        aria-busy={save.isPending}
        tabIndex={-1}
        onKeyDown={onPanelKeyDown}
        className="relative ml-auto flex h-full w-full max-w-3xl flex-col border-l border-border bg-surface shadow-2xl focus:outline-none"
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 id={titleId} className="text-lg font-semibold">{isEdit ? 'Edit Alert' : 'Create Alert'}</h2>
            <p id={descId} className="text-xs text-muted">Alert wizard — step {stepIdx + 1} of {STEPS.length}</p>
          </div>
          <button type="button" aria-label="Close the alert wizard" disabled={save.isPending} onClick={() => onOpenChange(false)} className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-text disabled:opacity-50">
            <X className="h-5 w-5" aria-hidden />
          </button>
        </div>

        {/* Step nav */}
        <div className="shrink-0 overflow-x-auto border-b border-border bg-surface2/40 px-3 py-2">
          <div className="flex min-w-max gap-1">
            {STEPS.map((st, i) => {
              const Icon = st.icon
              const active = step === st.id
              const done = i < stepIdx
              return (
                <button
                  key={st.id}
                  type="button"
                  disabled={save.isPending}
                  onClick={() => setStep(st.id)}
                  className={cn(
                    'flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors',
                    active ? 'bg-primary text-white' : done ? 'text-primary hover:bg-primary/10' : 'text-muted hover:bg-surface2 hover:text-text',
                  )}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {st.label}
                </button>
              )
            })}
          </div>
        </div>

        {/* Body */}
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {step === 'properties' && (
              <div className="space-y-4">
                <SectionTitle title="Alert Properties" hint="Name, severity, priority, and whether the rule is enabled." />
                <FormField label="Alert name" required>
                  <Input required value={s.name} onChange={(e) => setS({ ...s, name: e.target.value })} placeholder="Email me when a core router goes down" />
                </FormField>
                <FormField label="Description">
                  <Textarea value={s.description} onChange={(e) => setS({ ...s, description: e.target.value })} placeholder="Optional description for operators" rows={2} />
                </FormField>
                <FormField label="Severity">
                  <div className="flex gap-2">
                    {(['info', 'warning', 'critical'] as const).map((sev) => (
                      <button
                        key={sev}
                        type="button"
                        onClick={() => setS({ ...s, severity: sev })}
                        className={cn(
                          'flex-1 rounded-lg border px-3 py-2 text-xs font-semibold capitalize',
                          s.severity === sev ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text',
                        )}
                      >
                        {sev}
                      </button>
                    ))}
                  </div>
                </FormField>
                <FormField
                  label="Priority"
                  hint={s.priority
                    ? `${PRIORITY_META[s.priority].label}: ${PRIORITY_META[s.priority].trigger}`
                    : `${autoPriorityText(s.severity)} P1 is never derived — choose it only for a confirmed major outage.`}
                >
                  <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Incident priority">
                    {(['', ...PRIORITIES] as const).map((p) => (
                      <button
                        key={p || 'auto'}
                        type="button"
                        role="radio"
                        aria-checked={s.priority === p}
                        onClick={() => setS({ ...s, priority: p })}
                        title={p ? PRIORITY_META[p].trigger : 'Derived from the severity when the incident is raised'}
                        className={cn(
                          'rounded-lg border px-3 py-2 text-xs font-semibold',
                          s.priority === p ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text',
                        )}
                      >
                        {p ? PRIORITY_META[p].label : `Auto (${derivePriority(s.severity)})`}
                      </button>
                    ))}
                  </div>
                </FormField>
                <div className="flex items-center justify-between rounded-lg border border-border px-4 py-3">
                  <div>
                    <div id={`${titleId}-enabled`} className="text-sm font-medium">Enabled</div>
                    <div id={`${titleId}-enabled-h`} className="text-xs text-muted">Rule evaluates immediately when enabled</div>
                  </div>
                  <Switch checked={s.enabled} onCheckedChange={(v) => setS({ ...s, enabled: v })}
                    aria-labelledby={`${titleId}-enabled`} aria-describedby={`${titleId}-enabled-h`} />
                </div>
              </div>
            )}

            {step === 'trigger' && (
              <div className="space-y-4">
                <SectionTitle title="Trigger Conditions" hint="Define when this alert fires. Add multiple child conditions with AND/OR logic." />
                <div className="grid grid-cols-2 gap-1 rounded-lg border border-border bg-surface2 p-1 text-xs sm:grid-cols-4 lg:grid-cols-6">
                  {RULE_SOURCES.map(({ value: src, label: srcLabel, hint: srcHint }) => (
                    <button
                      key={src}
                      type="button"
                      disabled={isEdit && (src === 'server') !== editingServerRule}
                      title={isEdit && (src === 'server') !== editingServerRule
                        ? (editingServerRule ? 'A server rule stays a server rule. Create a new rule to watch something else.' : 'Create a new rule to watch servers.')
                        : srcHint}
                      onClick={() => {
                        if (src === s.source) return
                        // Entering/leaving APM swaps the condition metric domain,
                        // and `target` changes meaning (service name vs interface).
                        if (src === 'server') {
                          // A device scope (group, tag) does not carry over: a new server rule covers all servers.
                          setS({ ...s, source: src, target: '', server_id: '', server_scope: 'all', group_id: '', scope_tag: '', device_id: '', device_type: '', location: '',
                            min_duration: 300, clear_duration: null, reset_mode: 'auto', recovery_alert: true,
                            escalation_mode: s.escalation_mode === 'legacy' ? 'default' : s.escalation_mode,
                            conditions: [{ metric: 'host_cpu_pct', operator: '>', threshold: 90, reset_threshold: null }] })
                        } else if (src === 'syslog') {
                          setS({ ...s, source: src, target: '', reset_mode: 'none', conditions: [{ metric: 'syslog', operator: '<=', threshold: 4 }] })
                        } else if (src === 'apm') {
                          setS({ ...s, source: src, target: '', conditions: [{ metric: 'apm_latency_p95', operator: '>', threshold: 800 }] })
                        } else if (src === 'netflow') {
                          setS({ ...s, source: src, target: '', reset_mode: 'auto', recovery_alert: true })
                        } else if (['service', 'gap', 'appliance', 'virtualization', 'udt', 'netpath'].includes(src)) {
                          // One condition on the family's first metric; no device scope carries over.
                          const metric = src === 'service' ? 'service_status' : metricsFor(src)[0].key
                          const event = ruleTimers({ metric }).closes === 'expires'
                          setScopeMode('all')
                          setS({ ...s, source: src, target: '', server_id: '', server_scope: 'all', device_id: '', group_id: '', device_type: '',
                            location: '', scope_tag: '', service_check_id: '', service_check_group_id: '', profile_id: '', if_role: '',
                            trigger_on: src === 'service' ? (s.source === 'service' ? s.trigger_on : 'down') : 'any',
                            min_duration: 0, clear_duration: null, reset_mode: event ? 'none' : 'auto', recovery_alert: !event,
                            conditions: [src === 'service' ? { metric, operator: '==', threshold: 0, reset_threshold: null } : newCondition(metric)] })
                        } else if (s.source === 'server') {
                          // Nor does a server tag / group scope carry over to a device rule.
                          setScopeMode('all')
                          setS({ ...s, source: src, target: '', server_id: '', server_scope: 'all', group_id: '', scope_tag: '',
                            conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }], condition_tree: null })
                        } else if (['apm', 'syslog', 'netflow', 'service', 'gap', 'appliance', 'virtualization', 'udt', 'netpath'].includes(s.source)) {
                          setS({ ...s, source: src, target: '', server_id: '', conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }] })
                        } else {
                          setS({ ...s, source: src })
                        }
                      }}
                      aria-pressed={s.source === src}
                      className={cn('flex-1 rounded-md px-1 py-2 font-medium disabled:cursor-not-allowed disabled:opacity-40', s.source === src ? 'bg-primary text-white' : 'text-muted')}
                    >
                      {srcLabel}
                    </button>
                  ))}
                </div>

                {s.source === 'service' && (
                  <FormField label="Watch" hint="A status change pages on the check's confirmed state; certificate and response-time rules compare a reading with a limit.">
                    <Select value={first0?.metric === 'service_status' ? 'service_status' : first0?.metric}
                      onValueChange={(v) => setS({ ...s, trigger_on: v === 'service_status' ? 'down' : 'any', device_id: '', scope_tag: '',
                        min_duration: 0, clear_duration: null,
                        conditions: [v === 'service_status' ? { metric: v, operator: '==', threshold: 0, reset_threshold: null } : newCondition(v)] })}>
                      <SelectTrigger aria-label="What the service check rule watches"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {metricsFor('service').map((m) => <SelectItem key={m.key} value={m.key}>{m.label}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </FormField>
                )}
                {familySource && (
                  <FamilyConditionFields s={s} newRule={!isEdit} hideWatch={s.source === 'service'} onChange={(patch) => setS((st) => ({ ...st, ...patch }))} />
                )}
                {s.source === 'service' && first0?.metric === 'service_status' && (
                  <FormField label="Trigger on status" hint="Fires when a service check transitions to this state">
                    <Select value={s.trigger_on} onValueChange={(v: any) => setS({ ...s, trigger_on: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="down">Service goes down</SelectItem>
                        <SelectItem value="up">Service comes up</SelectItem>
                        <SelectItem value="degraded">Service degraded</SelectItem>
                        <SelectItem value="any">Any status change</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                )}

                {s.source === 'trap' && (
                  <FormField label="Trap OID filter (optional)" hint="Empty = any trap. Prefix match supported.">
                    <Input value={s.trap_oid} onChange={(e) => setS({ ...s, trap_oid: e.target.value })} placeholder="1.3.6.1.6.3.1.1.5.3" />
                  </FormField>
                )}

                {s.source === 'syslog' && <div className="space-y-3">
                  <FormField label="Message contains (optional)"><Input value={s.target} onChange={e => setS({ ...s, target: e.target.value })} /></FormField>
                  <FormField label="Severity condition" hint="0 is emergency; 7 is debug. One open alert per device and rule; it closes by itself after the event alert expiry window.">
                    <div className="flex gap-2"><select aria-label="Syslog severity operator" value={s.conditions[0].operator} onChange={e => setS({ ...s, conditions: [{ ...s.conditions[0], operator: e.target.value }] })} className="rounded border border-border bg-surface p-2">
                      {['<=', '<', '==', '!=', '>=', '>'].map(op => <option key={op}>{op}</option>)}
                    </select><Input aria-label="Syslog severity threshold" type="number" min={0} max={7} value={s.conditions[0].threshold} onChange={e => setS({ ...s, conditions: [{ ...s.conditions[0], threshold: Number(e.target.value) }] })} /></div>
                  </FormField>
                </div>}
                {s.source === 'device' && (
                  <>
                    {s.conditions.some((c) => c.metric === 'ping_status') && (
                      <FormField label="Status transition" hint="For ping/status rules — when device changes state">
                        <Select value={s.trigger_on} onValueChange={(v: any) => setS({ ...s, trigger_on: v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="down">Device goes down</SelectItem>
                            <SelectItem value="up">Device comes up</SelectItem>
                            <SelectItem value="degraded">Device degraded</SelectItem>
                            <SelectItem value="any">Any status change</SelectItem>
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-muted">Child conditions</span>
                      {s.conditions.length > 1 && (
                        <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
                          {(['AND', 'OR'] as const).map((l) => (
                            <button key={l} type="button" onClick={() => setS({ ...s, condition_logic: l })}
                              className={cn('rounded px-2.5 py-1 font-medium', s.condition_logic === l ? 'bg-primary text-white' : 'text-muted')}>
                              {l === 'AND' ? 'All (AND)' : 'Any (OR)'}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    {stateInfo ? (
                      // A template state (enum) metric: the states that alert, stored as AND-combined conditions.
                      <div className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-3">
                        <TemplateMetricPicker metric={first0.metric} templates={templates} scopeTemplateId={s.profile_id}
                          onPick={(m, templateId) => pickTemplateMetric(0, m, templateId)} />
                        <StatePicker info={stateInfo} conditions={s.conditions}
                          onChange={(conds) => setS((st) => ({ ...st, conditions: conds, condition_logic: 'AND' }))} />
                      </div>
                    ) : s.conditions.map((c, i) => {
                      const spec = metricSpec(c.metric)
                      const isTpl = c.metric.startsWith('tpl_') && c.metric !== 'tpl_'
                      const event = spec?.kind === 'event' && !!spec.fixed
                      const auto = autoResetThreshold(c)
                      // A new percent rule starts with a clear level 5 points below the limit (plan step 2.2).
                      const fresh = isEdit ? null : newPercentClearLevel(c)
                      const resettable = (isTpl || (spec?.source === 'device' && spec.kind === 'level' && !spec.fixed && spec.clearLevel !== false
                        && c.metric !== 'if_oper_status')) && ['>', '>=', '<', '<='].includes(c.operator)
                      return (
                      <div key={i} className="flex items-end gap-2 rounded-lg border border-border/60 bg-surface2/30 p-3">
                        <div className="grid flex-1 grid-cols-3 gap-2">
                          <FormField label={i === 0 ? 'Field' : ' '}>
                            <Select value={c.metric} onValueChange={(v) => {
                              const next = metricSpec(v)
                              if (next?.single) {
                                // Flapping, speed change and F5 certificates are rules of their own (the server refuses others).
                                setS((st) => ({ ...st, condition_logic: 'AND', conditions: [newCondition(v)], target: INTERFACE_METRICS.has(v) ? st.target : '',
                                  min_duration: v === 'if_speed_changed' ? 0 : st.min_duration }))
                              } else if (v === REBOOT_METRIC) {
                                // An event: the stored comparison is fixed and a breach hold would never be met.
                                setS((st) => ({ ...st, min_duration: s.conditions.length === 1 ? 0 : st.min_duration,
                                  conditions: st.conditions.map((x, idx) => (idx === i ? { metric: v, operator: '==', threshold: 1, reset_threshold: null } : x)) }))
                              } else if (v === 'tpl_') {
                                updateCond(i, { metric: v })
                              } else {
                                const ops = operatorsFor(v)
                                const fromEvent = !!metricSpec(c.metric)?.fixed
                                updateCond(i, ops.includes(c.operator) && !fromEvent
                                  ? { metric: v }
                                  : { metric: v, operator: next?.defaults?.operator ?? ops[0], threshold: next?.defaults?.threshold ?? c.threshold, reset_threshold: null })
                              }
                            }}>
                              <SelectTrigger><SelectValue /></SelectTrigger>
                              <SelectContent>
                                {metricsFor('device', c.metric).map((m) => (
                                  <SelectItem key={m.key} value={m.key}>{m.label}{m.retired ? ' (retired)' : ''}</SelectItem>
                                ))}
                                {isTpl && (
                                  <SelectItem value={c.metric}>Template: {tplNames[c.metric.slice(4)] || c.metric.slice(4)}</SelectItem>
                                )}
                                <SelectItem value="tpl_">Template metric…</SelectItem>
                              </SelectContent>
                            </Select>
                          </FormField>
                          {event ? (
                            <p className="col-span-2 self-center text-xs text-muted">
                              {c.metric === REBOOT_METRIC
                                ? <>Fires when the device&apos;s uptime goes back to zero (it restarted). There is no value to compare.</>
                                : spec?.hint}
                            </p>
                          ) : (<>
                          <FormField label={i === 0 ? 'Operator' : ' '}>
                            <Select value={c.operator} onValueChange={(v) => updateCond(i, { operator: v })}>
                              <SelectTrigger aria-label={`Operator of condition ${i + 1}`}><SelectValue /></SelectTrigger>
                              <SelectContent>
                                {(isTpl || c.metric === 'tpl_' ? [...ALL_OPS] : operatorsFor(c.metric, c.operator)).map((op) => (
                                  <SelectItem key={op} value={op}>{op}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </FormField>
                          <FormField label={i === 0 ? `Value${spec?.unit ? ` (${spec.unit.trim()})` : ''}` : ' '}>
                            <Input type="number" step="any" min={spec?.min} max={spec?.max} value={c.threshold} onChange={(e) => updateCond(i, { threshold: Number(e.target.value) })} />
                          </FormField>
                          </>)}
                          {resettable && (
                            <FormField label="Reset value (optional)" hint={fresh != null
                              ? `Empty = ${fresh}${spec?.unit === '%' ? '%' : ''} for a new rule: clears 5 points on the healthy side of the limit.`
                              : auto != null ? `Empty = auto: clears ${c.operator.startsWith('>') ? 'below' : 'above'} ${auto}%`
                              : spec?.clearLevel ? `Empty = ${spec.clearLevel.auto}.` : undefined}>
                              <Input type="number" step="any" placeholder={fresh != null ? `${fresh}` : auto != null ? `auto (${auto}%)` : spec?.clearLevel ? 'auto' : 'Same as trigger'} value={c.reset_threshold ?? ''} onChange={e => updateCond(i, { reset_threshold: e.target.value === '' ? null : Number(e.target.value) })} />
                            </FormField>
                          )}
                        {c.metric.startsWith('tpl_') && (
                          <TemplateMetricPicker className="col-span-3" metric={c.metric} templates={templates} scopeTemplateId={s.profile_id}
                            onPick={(m, templateId) => pickTemplateMetric(i, m, templateId)} />
                        )}
                        {spec && (spec.retired || (spec.hint && !event)) && (
                          <p className={cn('col-span-3 text-xs', spec.retired && isRetired(c.metric) ? 'text-warning' : 'text-muted')}>{spec.retired || spec.hint}</p>
                        )}
                        </div>
                        <Button type="button" variant="ghost" size="icon" className="h-9 w-9 text-muted hover:text-danger" disabled={s.conditions.length === 1} onClick={() => removeCond(i)}>
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                      )
                    })}
                    {!stateInfo && !s.conditions.some((c) => metricSpec(c.metric)?.single) && (
                      <Button type="button" variant="outline" size="sm" onClick={addCond}><Plus className="h-3.5 w-3.5" />Add condition</Button>
                    )}
                    {s.conditions.some((c) => INTERFACE_METRICS.has(c.metric)) && (
                      <div className="grid gap-3 sm:grid-cols-2">
                        <FormField label="Interface filter" hint="Name substring or if_index. Empty = all interfaces.">
                          <Input value={s.target} onChange={(e) => setS({ ...s, target: e.target.value })} placeholder="GigabitEthernet0/1" />
                        </FormField>
                        {interfaceRoleApplies(s.conditions) && (
                          <FormField label="Interface role" hint="Roles come from the device's template, or an operator's choice on the interface.">
                            <Select value={s.if_role || '__any__'} onValueChange={(v) => setS({ ...s, if_role: v === '__any__' ? '' : v })}>
                              <SelectTrigger><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="__any__">Any role</SelectItem>
                                {INTERFACE_ROLES.map((r) => <SelectItem key={r} value={r}>{ROLE_LABEL[r]}</SelectItem>)}
                              </SelectContent>
                            </Select>
                          </FormField>
                        )}
                      </div>
                    )}
                  </>
                )}

                {isServer && s.condition_tree && (
                  <ServerConditionTree tree={s.condition_tree}
                    onChange={(tree) => setS((st) => ({ ...st, condition_tree: tree }))}
                    onSingle={() => setS((st) => (st.condition_tree ? { ...st, ...singleFromTree(st.condition_tree), condition_tree: null } : st))} />
                )}
                {isServer && !s.condition_tree && (() => {
                  const c = s.conditions[0]
                  const spec = serverMetric(c.metric)
                  const unit = spec?.unit ?? ''
                  const auto = serverKind === 'pct' ? serverAutoReset(c.operator, Number(c.threshold), c.metric) : null
                  // A new percent rule clears 5 points on the healthy side unless set (plan step 2.2).
                  const freshServer = serverKind === 'pct' && !isEdit ? newPercentClearLevel(c) : null
                  return (
                    <div className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-3">
                      <div className={cn('grid gap-2', serverKind === 'pct' ? 'grid-cols-3' : 'grid-cols-1')}>
                        <FormField label="Watch">
                          <Select value={c.metric} onValueChange={(v) => setS({ ...s, ...serverWatchChange(s, v) })}>
                            <SelectTrigger><SelectValue /></SelectTrigger>
                            <SelectContent>
                              {SERVER_METRICS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
                            </SelectContent>
                          </Select>
                        </FormField>
                        {serverKind === 'pct' && (
                          <>
                            <FormField label="Operator">
                              <Select value={c.operator} onValueChange={(v) => updateCond(0, { operator: v })}>
                                <SelectTrigger aria-label="Server rule operator"><SelectValue /></SelectTrigger>
                                <SelectContent>
                                  {[...serverOperators(c.metric), ...(serverOperators(c.metric).includes(c.operator) ? [] : [c.operator])].map((op) => <SelectItem key={op} value={op}>{op}</SelectItem>)}
                                </SelectContent>
                              </Select>
                            </FormField>
                            <FormField label={`Value${unit ? ` (${unit.trim()})` : ''}`}>
                              <Input type="number" min={spec?.min} max={spec?.max} step="any" value={c.threshold} onChange={(e) => updateCond(0, { threshold: Number(e.target.value) })} />
                            </FormField>
                          </>
                        )}
                      </div>
                      {serverKind === 'pct' && (
                        <FormField label="Reset value (optional)" hint={freshServer != null
                          ? `Empty = ${freshServer}% for a new rule: clears 5 points on the healthy side of the limit.`
                          : auto != null ? `Empty = auto: clears ${c.operator.startsWith('>') ? 'below' : 'above'} ${auto}${unit}` : undefined}>
                          <Input type="number" step="any" className="w-40" placeholder={freshServer != null ? String(freshServer) : auto != null ? `auto (${auto}${unit})` : 'Same as trigger'} value={c.reset_threshold ?? ''}
                            onChange={(e) => updateCond(0, { reset_threshold: e.target.value === '' ? null : Number(e.target.value) })} />
                        </FormField>
                      )}
                      {(serverKind === 'svc' || serverKind === 'proc') && (
                        <FormField label={serverKind === 'svc' ? 'Service name' : 'Process name'} required
                          hint={serverKind === 'svc' ? 'The Windows service or systemd unit name, as the agent reports it.' : 'The executable name, as the agent reports it.'}>
                          <Input value={s.target} onChange={(e) => setS({ ...s, target: e.target.value })} placeholder={serverKind === 'svc' ? 'e.g. MSSQLSERVER' : 'e.g. nginx.exe'} />
                        </FormField>
                      )}
                      {serverKind === 'auto' && (
                        <FormField label="Services to leave out (patterns)"
                          hint="Optional. Service names or display names, comma separated, wildcards allowed, case-insensitive (e.g. gupdate*, edgeupdate*). A service left out resolves its open alert without a recovery notice.">
                          <Input value={s.target} onChange={(e) => setS({ ...s, target: e.target.value })} placeholder="gupdate*, edgeupdate*" aria-label="Services to leave out" />
                        </FormField>
                      )}
                      {spec?.hint && <p className="text-xs text-muted">{spec.hint}</p>}
                      {serverKind === 'event' && <p className="text-xs text-muted">Notifies when the agent reports a new OS boot, restart request or shutdown, with the time it happened. A further restart while the alert is open updates the same alert. Historical backfill does not page. Shutdown evidence may arrive after the next boot.</p>}
                      {serverKind === 'flag' && (
                        <p className="text-xs text-muted">
                          Fires when a server&apos;s agent stops reporting but the server still answers ping: the server is up and
                          its monitoring is lost. This needs the server to be ping-monitored as well (a device with the same IP,
                          or linked to it). A server that is silent and does not answer ping, or has no ping monitoring, is
                          paged by <span className="font-medium text-text">Device Down</span> instead.
                        </p>
                      )}
                      {(serverKind === 'pct' || serverKind === 'svc' || serverKind === 'proc' || serverKind === 'auto') && (
                        <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-3">
                          <Button type="button" variant="outline" size="sm"
                            onClick={() => setS((st) => ({ ...st, condition_tree: treeFromDraft(st), min_duration: st.min_duration || 300 }))}>
                            <Plus className="h-3.5 w-3.5" /> Add condition
                          </Button>
                          <span className="text-xs text-muted">Combine several conditions in one rule, for example CPU and memory both above 90%.</span>
                        </div>
                      )}
                    </div>
                  )
                })()}

                {s.source === 'apm' && (
                  <div className="flex items-end gap-2 rounded-lg border border-border/60 bg-surface2/30 p-3">
                    <div className={cn('grid flex-1 gap-2', apmEvent ? 'grid-cols-1' : 'grid-cols-3')}>
                      <FormField label="Trigger / metric">
                        <Select value={s.conditions[0]?.metric} onValueChange={(v) => {
                          const switchesDataPlane = apmScopeKind(v) !== apmScopeKind(s.conditions[0]?.metric)
                          setS({
                            ...s,
                            target: switchesDataPlane ? '' : s.target,
                            min_duration: isApmEvent(v) ? 0 : s.min_duration,
                            clear_duration: isApmEvent(v) ? null : s.clear_duration,
                            conditions: [{ ...s.conditions[0], metric: v,
                              ...(isApmEvent(v) ? { operator: '==', threshold: 1, reset_threshold: null } : {}) }],
                          })
                        }}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {APM_METRICS.map((m) => (
                              <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                      {!apmEvent && <><FormField label="Operator">
                        <Select value={s.conditions[0]?.operator} onValueChange={(v) => updateCond(0, { operator: v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {['>', '>=', '<', '<=', '==', '!='].map((op) => (
                              <SelectItem key={op} value={op}>{op}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                      <FormField label="Threshold">
                        <Input type="number" step="any" value={s.conditions[0]?.threshold} onChange={(e) => updateCond(0, { threshold: Number(e.target.value) })} />
                      </FormField></>}
                    </div>
                    {apmEvent && <p className="max-w-sm text-xs text-muted">{apmEventInfo.hint}</p>}
                  </div>
                )}

                {s.source === 'netflow' && (
                  <NetflowConditionFields form={s.nf} errors={nfErrors} onChange={(nf) => setS((st) => ({ ...st, nf }))} />
                )}

                {!apmEvent && !eventAutoClear && s.source !== 'trap' && s.source !== 'syslog' && s.source !== 'netflow' && !(isServer && serverKind !== 'pct' && serverKind !== 'flag' && serverKind !== 'auto')
                  && !(familySource && !timers.hold) && (
                  serverKind === 'flag' ? (
                    <FormField label="Agent silent for" hint={`How long the agent may miss its heartbeats (every 30 seconds) before the alert is raised. 0 = the default ${DEFAULT_SILENCE_SECONDS} seconds; never less than ${MIN_SILENCE_SECONDS}.`}>
                      <div className="flex items-center gap-3">
                        <Input type="number" min={0} value={s.min_duration} onChange={(e) => setS({ ...s, min_duration: Number(e.target.value) })} className="w-28" />
                        <span className="text-sm text-muted">seconds (0 = {DEFAULT_SILENCE_SECONDS})</span>
                      </div>
                    </FormField>
                  ) : flapRule ? (
                    <FormField label="Window" hint={`State changes are counted over this window; it takes the place of the breach hold. Empty or 0 = ${DEFAULT_FLAP_WINDOW_S / 60} minutes. An override's hold changes the window for its assets.`}>
                      <div className="flex items-center gap-3">
                        <Input type="number" min={0} placeholder={String(DEFAULT_FLAP_WINDOW_S)} value={s.min_duration || ''} onChange={(e) => setS({ ...s, min_duration: Math.max(0, Number(e.target.value) || 0) })} className="w-28" aria-label="Flapping window in seconds" />
                        <span className="text-sm text-muted">seconds</span>
                      </div>
                    </FormField>
                  ) : s.source === 'apm' ? (
                    <FormField label="Averaging window" hint={`The metric is averaged over this window and judged every minute; never less than ${APM_MIN_WINDOW_S} seconds. APM rules have no separate breach or clear hold.`}>
                      <div className="flex items-center gap-3">
                        <Input type="number" min={0} value={s.min_duration} onChange={(e) => setS({ ...s, min_duration: Number(e.target.value) })} className="w-28" aria-label="Averaging window in seconds" />
                        <span className="text-sm text-muted">seconds (0 = {APM_MIN_WINDOW_S})</span>
                      </div>
                    </FormField>
                  ) : (
                    <FormField label="Condition must exist for" hint={statusRule
                      ? `Counted from the confirmed status change. Device status already needs several checks before it changes (Settings → Monitoring), so a hold here adds to that${flapHold ? '; while a device\'s up/down or latency/loss changes flap, its status alerts for that kind of change are also held automatically until a status lasts the flap stability time' : ''}.`
                      : first0?.metric === 'service_response_ms'
                      ? 'Hold time before firing; the response time is also averaged over this time (at least 5 minutes).'
                      : 'Hold time before firing: the condition must persist this long before the alert is raised.'}>
                      <div className="flex items-center gap-3">
                        <Input type="number" min={0} value={s.min_duration} onChange={(e) => setS({ ...s, min_duration: Number(e.target.value) })} className="w-28" />
                        <span className="text-sm text-muted">seconds (0 = fire immediately)</span>
                      </div>
                    </FormField>
                  )
                )}
                {(eventAutoClear || isLifecycle || first0?.metric === 'appliance_admin_changes') && (() => {
                  const noun = rebootRule ? 'reboot' : speedRule ? 'speed change' : first0?.metric === 'appliance_admin_changes' ? 'administrative change'
                    : s.conditions[0]?.metric === 'host_shutdown' ? 'shutdown' : 'restart'
                  const again = rebootRule ? 'rebooted again' : speedRule ? 'changed speed again' : first0?.metric === 'appliance_admin_changes' ? 'one more change' : 'restarted again'
                  return (
                    <FormField
                      label="Auto-clear after"
                      hint={`The ${noun} alert closes on its own once this long passes without another one; a further ${noun} in the meantime updates the same alert ("${again}") and starts the time again. You can also resolve it by hand at any time. Empty = ${DEFAULT_EVENT_HOLD_SECONDS / 60} minutes.`}
                    >
                      <div className="flex items-center gap-3">
                        <Input
                          type="number" min={0} className="w-28" placeholder={String(DEFAULT_EVENT_HOLD_SECONDS)}
                          value={s.clear_duration ?? ''}
                          onChange={(e) => setS({ ...s, clear_duration: e.target.value === '' ? null : Math.max(0, Number(e.target.value)) })}
                        />
                        <span className="text-sm text-muted">seconds after the last {rebootRule ? 'reboot' : noun === 'shutdown' || noun === 'restart' ? 'event' : noun}</span>
                      </div>
                    </FormField>
                  )
                })()}
                {!eventAutoClear && (usesMetricLifecycle(s) || serverKind === 'pct' || serverKind === 'auto' || (familySource && timers.clear === 'clear')) && (() => {
                  // A new percent rule clears after 900 s unless set (plan step 2.2); existing rules keep the 300 s default.
                  const freshPercent = !isEdit && s.conditions.some((c) => newPercentClearLevel({ ...c, reset_threshold: null }) != null)
                  const fallback = freshPercent ? NEW_PERCENT_CLEAR_S : DEFAULT_CLEAR_SECONDS
                  return (
                    <FormField
                      label={flapRule ? 'Extra clear hold' : 'Clear after'}
                      hint={flapRule
                        ? 'The alert clears once a whole window passes without a state change, plus this long when set. Missing polls never clear it.'
                        : s.source === 'gap'
                        ? `Clears once data is delivered again and keeps arriving this long. A gap that is still open but below the limit neither raises nor clears. Empty = ${fallback} seconds.`
                        : `The cleared condition must hold this long before the alert resolves; a breach in the meantime continues the same alert without a new notification. Missing data never counts as cleared. Empty = ${fallback} seconds${freshPercent ? ' for a new percent rule' : ''}.`}
                    >
                      <div className="flex items-center gap-3">
                        <Input
                          type="number" min={0} className="w-28" placeholder={flapRule ? '0' : String(fallback)}
                          value={s.clear_duration ?? ''}
                          onChange={(e) => setS({ ...s, clear_duration: e.target.value === '' ? null : Math.max(0, Number(e.target.value)) })}
                        />
                        <span className="text-sm text-muted">seconds (0 = resolve on the first healthy reading)</span>
                      </div>
                    </FormField>
                  )
                })()}
                {familySource && !timers.hold && !timers.clear && (
                  <p className="text-xs text-muted">
                    {timers.closes === 'expires'
                      ? `Each event raises or updates an alert at once; there is no hold. ${expiryText}`
                      : first0?.metric === 'netpath_path_change'
                      ? 'Each route change raises an alert at once; repeats within the cooldown are not paged again.'
                      : 'Judged on every evaluation (every 30 seconds) with no hold: raised on the first breach, resolved on the first healthy reading.'}
                  </p>
                )}
                {flapHold && (
                  <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2">
                    <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-primary" checked={s.notify_while_flapping} onChange={(e) => setS({ ...s, notify_while_flapping: e.target.checked })} />
                    <span className="text-xs">
                      <span className="font-medium">Notify immediately even while the device is flapping</span><br />
                      <span className="text-muted">Skips the automatic flapping hold for this rule, for example for core routers. Flapping started and stopped notices are not affected.</span>
                    </span>
                  </label>
                )}
              </div>
            )}

            {step === 'reset' && (
              <div className="space-y-4">
                <SectionTitle title="Reset Conditions" hint="How the notice closes, and whether a closing notification is sent." />
                {!eventOnly && (
                  <p className="text-sm text-muted">
                    {isLifecycle || rebootRule
                      ? `The alert closes on its own ${holdLabel(s.clear_duration)} after the last ${rebootRule ? 'reboot' : 'event'} (Auto-clear after), or when someone resolves it. The closing notice says what is known now: ${rebootRule ? 'how long the device has stayed up, or that it has no current uptime reading' : 'whether the server is reporting normally again, or that its agent is still silent'}. These options control the notification only.`
                      : speedRule || first0?.metric === 'appliance_admin_changes'
                      ? `The alert closes on its own ${holdLabel(s.clear_duration)} after the last ${speedRule ? 'speed change' : 'change'} (Auto-clear after), or when someone resolves it. These options control the notification only.`
                      : timers.closes === 'status'
                      ? 'The alert resolves when the confirmed status recovers, even when recovery notifications are off. These options control the notification only.'
                      : 'Alerts resolve according to their clear conditions, even when recovery notifications are off. These options control the notification only.'}
                  </p>
                )}
                <div className="space-y-2">
                  {eventOnly ? <p className="text-sm text-muted">{first0?.metric === 'netpath_path_change'
                    ? 'A route change is an event: its alert stays open until someone resolves it. No recovery notification is sent.'
                    : `${expiryText} No recovery notification is sent.`}</p> : [
                    { id: 'auto' as const, title: isLifecycle || rebootRule ? 'Notify when the alert closes' : 'Send a recovery notification', desc: isLifecycle || rebootRule ? 'Notify the configured routes when the alert closes after the auto-clear time.' : 'Notify the configured routes after the alert condition clears.' },
                    { id: 'none' as const, title: isLifecycle || rebootRule ? 'No closing notification' : 'No recovery notification', desc: 'The alert still closes in the system; no closing notification is sent.' },
                  ].map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => setS({ ...s, reset_mode: opt.id, recovery_alert: opt.id === 'auto' })}
                      className={cn(
                        'w-full rounded-lg border p-4 text-left transition-colors',
                        s.reset_mode === opt.id ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        {s.reset_mode === opt.id && <Check className="h-4 w-4 text-primary" />}
                        <span className="text-sm font-semibold">{opt.title}</span>
                      </div>
                      <p className="mt-1 pl-6 text-xs text-muted">{opt.desc}</p>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {step === 'scope' && (
              <div className="space-y-4">
                <SectionTitle title="Scope" hint="Which objects this alert monitors." />
                {s.source === 'netflow' ? (
                  <div className="space-y-4">
                    <NetflowScopeFields form={s.nf} errors={nfErrors} open={open}
                      onChange={(nf) => setS((st) => ({ ...st, nf }))} />
                    <NetflowDryRun form={s.nf} />
                  </div>
                ) : s.source === 'apm' ? (
                  <FormField
                    label={apmEvent ? apmEventInfo.label : isRumMetric ? 'Browser application / environment' : 'Application service'}
                    hint={apmEvent ? apmEventInfo.hint : isRumMetric
                      ? 'Empty = every application and environment reporting browser events. Each environment is evaluated independently once per minute.'
                      : 'Empty = every service reporting traces. Evaluated per service every minute against the RED rollups.'}
                  >
                    <Select
                      value={s.target || '__all__'}
                      onValueChange={(v) => setS({ ...s, target: v === '__all__' ? '' : v })}
                    >
                      <SelectTrigger><SelectValue placeholder={apmEvent ? apmEventInfo.all : isRumMetric ? 'All browser app environments' : 'All services'} /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__all__">{apmEvent ? apmEventInfo.all : isRumMetric ? 'All browser app environments' : 'All services'}</SelectItem>
                        {apmEvent ? apmEntities.map(entity => <SelectItem key={entity.id} value={entity.id}>{entity.name}</SelectItem>) : (isRumMetric ? rumApplications : apmServices).map((name) => (
                          <SelectItem key={name} value={name}>{name}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                ) : isServer ? (
                  <>
                    <FormField label="Apply to" hint={serverScope === 'tag' || serverScope === 'group'
                      ? 'Servers that get the tag or join the group later are covered automatically.'
                      : 'All servers covers every agent-monitored server, including servers added later.'}>
                      <Select value={serverScope} onValueChange={(v) => setS({ ...s, ...serverScopeChange(v as ServerScopeMode, s) })}>
                        <SelectTrigger aria-label="Servers the rule applies to"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {SERVER_SCOPE_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </FormField>
                    {(serverScope === 'tag' || serverScope === 'group') && (
                      // Tag and group combine: a server must carry the tag and be in the group. The one the scope
                      // is named after comes first; the other is optional.
                      <div className={cn('flex gap-3', serverScope === 'group' ? 'flex-col-reverse' : 'flex-col')}>
                        <FormField
                          label={serverScope === 'tag' ? 'Tag' : 'And with tag (optional)'}
                          hint="A user limited to tags can create rules for their own tags."
                        >
                          <TagPicker
                            value={s.scope_tag ? [s.scope_tag] : []}
                            // One tag: picking another replaces it.
                            onChange={(next) => setS({ ...s, scope_tag: next.length ? next[next.length - 1] : '' })}
                            placeholder={s.scope_tag ? 'Replace the tag…' : 'Choose a tag…'}
                            allowCreate
                          />
                        </FormField>
                        <FormField
                          label={serverScope === 'group' ? 'Device group' : 'And in group (optional)'}
                          hint="Static or dynamic; members of its subgroups are included."
                        >
                          <Select
                            value={s.group_id || '__none__'}
                            onValueChange={(v) => setS({ ...s, group_id: v === '__none__' ? '' : v })}
                          >
                            <SelectTrigger aria-label="Device group"><SelectValue placeholder="Select group" /></SelectTrigger>
                            <SelectContent>
                              <SelectItem value="__none__" disabled={serverScope === 'group'}>
                                {serverScope === 'group' ? 'Select group' : 'Any group'}
                              </SelectItem>
                              {s.group_id && !deviceGroups.some(({ group: g }) => g.id === s.group_id) && (
                                <SelectItem value={s.group_id}>Group {s.group_id.slice(0, 8)}</SelectItem>
                              )}
                              {deviceGroups.map(({ group: g, depth }) => (
                                <SelectItem key={g.id} value={g.id}>
                                  <span style={{ paddingLeft: `${depth * 0.75}rem` }}>{g.name}</span>
                                  <span className="ml-1.5 text-2xs text-muted">
                                    · {g.kind === 'dynamic' ? 'dynamic' : 'static'}{(g.asset_kinds || []).includes('server') ? '' : ', devices only'}
                                  </span>
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </FormField>
                      </div>
                    )}
                    {s.server_id && (
                      <FormField label="Server">
                        <Select value={s.server_id} onValueChange={(v) => setS({ ...s, server_id: v })}>
                          <SelectTrigger><SelectValue placeholder="Select server" /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="__pick__" disabled>Select server</SelectItem>
                            {/* A rule scoped to a server that is not in the first page still shows as chosen. */}
                            {s.server_id !== '__pick__' && !servers.some((x: any) => x.id === s.server_id) && (
                              <SelectItem value={s.server_id}>Server {s.server_id.slice(0, 8)}</SelectItem>
                            )}
                            {servers.map((x: any) => (
                              <SelectItem key={x.id} value={x.id}>{x.display_name || x.hostname}{x.primary_ip ? ` · ${x.primary_ip}` : ''}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                  </>
                ) : familySource && (s.source !== 'gap' || first0?.metric === 'agent_no_metrics') ? (
                  <FamilyScopeFields s={s} open={open} onChange={(patch) => setS((st) => ({ ...st, ...patch }))}
                    devices={devices} serviceChecks={serviceChecks} serviceGroups={serviceGroups} channels={channels}
                    deviceGroups={deviceGroups.map(({ group: g, depth }) => ({ id: g.id, name: `${'· '.repeat(depth)}${g.name}` }))} />
                ) : s.source === 'service' ? (
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label="Service check">
                      <Select value={s.service_check_id || '__all__'} onValueChange={(v) => setS({ ...s, service_check_id: v === '__all__' ? '' : v, service_check_group_id: '' })}>
                        <SelectTrigger><SelectValue placeholder="Any" /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__all__">Any service check</SelectItem>
                          {serviceChecks.map((c: any) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </FormField>
                    <FormField label="Service group">
                      <Select value={s.service_check_group_id || '__all__'} onValueChange={(v) => setS({ ...s, service_check_group_id: v === '__all__' ? '' : v, service_check_id: '' })}>
                        <SelectTrigger><SelectValue placeholder="Any" /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__all__">Any group</SelectItem>
                          {serviceGroups.map((g: any) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </FormField>
                  </div>
                ) : (
                  <>
                    <FormField label="Apply to">
                      <Select value={scopeMode} onValueChange={(v: any) => {
                        setScopeMode(v)
                        setS({ ...s, device_id: '', group_id: '', device_type: '', location: '', scope_tag: '' })
                      }}>
                        <SelectTrigger><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="all">{downCoversServers(s) ? 'All devices and servers' : 'All devices'}</SelectItem>
                          <SelectItem value="device">Specific device</SelectItem>
                          <SelectItem value="group">Device group</SelectItem>
                          <SelectItem value="type">Device type</SelectItem>
                          <SelectItem value="location">Location</SelectItem>
                          {/* Device Tracker rules match the reporting switch by device, group, type or location only. */}
                          {s.source !== 'udt' && <SelectItem value="tag">Tag</SelectItem>}
                        </SelectContent>
                      </Select>
                    </FormField>
                    {downCoversServers(s) && (scopeMode === 'all' || scopeMode === 'tag') && (
                      <p className="rounded-lg border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
                        This rule also pages for agent-monitored servers{scopeMode === 'tag' ? ' carrying the tag' : ''}: a server whose
                        agent stops reporting, and that ping cannot show is up, counts as down. Limiting it to a device, group,
                        type or location makes it a device-only rule.
                      </p>
                    )}
                    {scopeMode === 'device' && (
                      <FormField label="Device">
                        <Select value={s.device_id || '__pick__'} onValueChange={(v) => setS({ ...s, device_id: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue placeholder="Select device" /></SelectTrigger>
                          <SelectContent>
                            {devices.map((d: any) => <SelectItem key={d.id} value={d.id}>{d.hostname}{d.ip_address ? ` · ${d.ip_address}` : ''}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'group' && (
                      <FormField label="Group">
                        <Select value={s.group_id || '__pick__'} onValueChange={(v) => setS({ ...s, group_id: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {(groups || []).map((g: any) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'type' && (
                      <FormField label="Device type">
                        <Select value={s.device_type || '__pick__'} onValueChange={(v) => setS({ ...s, device_type: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <DeviceTypeItems />
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'location' && (
                      <FormField label="Location">
                        <Select value={s.location || '__pick__'} onValueChange={(v) => setS({ ...s, location: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {locations.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'tag' && (
                      <FormField
                        label="Tag"
                        hint="Applies to every device carrying this tag — devices tagged later are covered automatically."
                      >
                        <Select value={s.scope_tag || '__pick__'} onValueChange={(v) => setS({ ...s, scope_tag: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue placeholder="Select tag" /></SelectTrigger>
                          <SelectContent>
                            {tags.map((t) => (
                              <SelectItem key={t.id} value={t.name}>
                                <span className="inline-flex items-center gap-1.5">
                                  <span
                                    className="h-2 w-2 shrink-0 rounded-full"
                                    style={{ background: tagColor(t.name, tagColors) }}
                                  />
                                  {t.name}
                                </span>
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {s.source === 'device' && templateScopeApplies(s.conditions) && (
                      <FormField label="Only devices on template"
                        hint="Also covers templates customised from it. Combined with the scope above: a device must match both.">
                        <Select value={s.profile_id || '__any__'} onValueChange={(v) => setS({ ...s, profile_id: v === '__any__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="__any__">Any template</SelectItem>
                            {s.profile_id && !templates.some((t) => t.id === s.profile_id) && (
                              <SelectItem value={s.profile_id}>Template {s.profile_id.slice(0, 8)}</SelectItem>
                            )}
                            {templates.map((t) => <SelectItem key={t.id} value={t.id}>{t.name}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                  </>
                )}
              </div>
            )}

            {step === 'actions' && (
              <div className="space-y-4">
                <SectionTitle title="Trigger Actions" hint="Notifications sent when the alert fires. Select channels and configure anti-flap settings." />
                <div className="grid grid-cols-2 gap-2">
                  {channels.length === 0 ? (
                    <p className="col-span-2 text-xs text-muted">No channels — <a href="/channels" className="text-primary hover:underline">create one</a></p>
                  ) : channels.map((c) => {
                    const checked = s.notify_channels.includes(c.id)
                    return (
                      <button key={c.id} type="button" onClick={() => toggleChannel(c.id)}
                        className={cn('flex items-center justify-between rounded-lg border px-3 py-2.5 text-left text-xs', checked ? 'border-primary bg-primary/10' : 'border-border')}>
                        <span><span className="font-medium">{c.name}</span> <span className="text-muted">{CHANNEL_TYPE_LABEL[c.type]}</span></span>
                        {checked && <Check className="h-3.5 w-3.5 text-primary" />}
                      </button>
                    )
                  })}
                </div>
                {s.notify_channels.length === 0 && (
                  <p className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                    No channel selected: this rule&apos;s alerts are only seen in ZenPlus unless an escalation policy routes them (Escalation step).
                  </p>
                )}
                <div className="grid grid-cols-2 gap-3">
                  {timers.cooldown ? (
                    <FormField label="Cooldown (seconds)" hint={eventOnly
                      ? 'A repeat of the same event while its alert is open pages again only after this long (at least 60 seconds).'
                      : 'Anti-flap: after a notification goes out, repeat triggers for the same object stay quiet this long'}>
                      <Input type="number" min={0} value={s.cooldown} onChange={(e) => setS({ ...s, cooldown: Number(e.target.value) })} />
                    </FormField>
                  ) : (
                    <p className="self-end rounded-lg border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
                      This rule type has no cooldown: each incident is notified once, and repeat pages come only from escalation.
                    </p>
                  )}
                  {!isServer && (
                    <FormField label="Max escalation repeats" hint="Cap on repeat pages from an escalation level (0 = unlimited)">
                      <Input type="number" min={0} value={s.max_repeat} onChange={(e) => setS({ ...s, max_repeat: Number(e.target.value) })} />
                    </FormField>
                  )}
                </div>
                {isServer ? (
                  <p className="rounded-lg border border-border bg-surface2/40 p-3 text-xs text-muted">
                    Server alerts are sent in the built-in wording: it names the server, what was measured and the rule&apos;s
                    condition. After saving, use <span className="text-text">Preview message</span> on the rule list to see it.
                  </p>
                ) : (<>
                <TemplateVars vars={templateVars} />
                <p className="text-xs text-muted">
                  This is the wording that will be sent. Edit any field to word it yourself; a field
                  left as the built-in text keeps tracking it, so improvements to the default wording
                  still reach this rule. Open <span className="text-text">Preview message</span> on the
                  rule to see it rendered with sample readings.
                </p>
                <TemplateField label="Email subject" field="email_subject"
                  value={s.email_subject} defaults={tplDefaults} onChange={setTemplate} />
                <TemplateField label="Email body" field="email_body" rows={5}
                  value={s.email_body} defaults={tplDefaults} onChange={setTemplate}
                  hint="The sentence in the highlighted callout. The rule, severity, device and time are added by the email layout." />
                <TemplateField label="SMS template" field="sms_template" rows={2}
                  value={s.sms_template} defaults={tplDefaults} onChange={setTemplate} />
                <LiveMail msg={livePreview?.alert} label="Trigger email" />
                </>)}
              </div>
            )}

            {step === 'escalation' && (
              <div className="space-y-4">
                <SectionTitle
                  title="Escalation Policy"
                  hint="Who is paged when the incident stays active and nobody acknowledges it. Acknowledging stops escalation; resolving sends an all-clear to everyone who was notified."
                />
                <div className={cn('grid gap-2', isServer ? 'sm:grid-cols-2' : 'sm:grid-cols-3')} role="radiogroup" aria-label="How this rule escalates">
                  {([
                    { v: 'default', label: 'Use priority default', hint: `The policy that is the default for ${effPriority}` },
                    { v: 'policy', label: 'Choose a policy', hint: 'Always this policy, whatever the priority' },
                    { v: 'legacy', label: 'Legacy tiers', hint: 'Channel tiers on this rule; they override the priority default' },
                  ] as const).filter((o) => !(isServer && o.v === 'legacy')).map((o) => (
                    <button
                      key={o.v}
                      type="button"
                      role="radio"
                      aria-checked={s.escalation_mode === o.v}
                      onClick={() => setS({ ...s, escalation_mode: o.v })}
                      className={cn('rounded-lg border px-3 py-2 text-left', s.escalation_mode === o.v ? 'border-primary bg-primary/10' : 'border-border hover:bg-surface2')}
                    >
                      <div className={cn('text-xs font-semibold', s.escalation_mode === o.v ? 'text-primary' : 'text-text')}>{o.label}</div>
                      <div className="text-xs text-muted">{o.hint}</div>
                    </button>
                  ))}
                </div>
                {s.escalation_mode === 'default' && (
                  <div className="rounded-lg border border-border bg-surface2/40 px-4 py-3 text-xs">
                    <div className="mb-1 flex items-center gap-2"><PriorityBadge priority={effPriority} />
                      <span className="text-muted">{s.priority ? 'chosen on this rule' : `derived from ${s.severity} severity`}</span>
                    </div>
                    {policiesQ.isLoading ? <span className="text-muted">Loading policies…</span> : policiesQ.isError ? (
                      <span className="text-danger">Could not load policies, so the {effPriority} default is unknown: {apiErrorMessage(policiesQ.error)}</span>
                    ) : defaultPolicy ? (
                      <span>Escalates through <span className="font-semibold text-text">{defaultPolicy.name}</span> <span className="text-muted">({stepSummary(defaultPolicy.steps)}{defaultPolicy.review_minutes ? `; updates every ${fmtMinutes(defaultPolicy.review_minutes)}` : ''})</span>.</span>
                    ) : (
                      <span className="text-warning">No policy is the default for {effPriority}: this rule&apos;s incidents notify only its trigger channels and do not escalate. Set one under Alerting → <a href="/escalation?tab=policies" className="underline">Escalation &amp; On-call</a>.</span>
                    )}
                    {s.escalation_levels.length > 0 && <p className="mt-1 text-muted">{tiersRemovedText(s.escalation_levels.length, 'default', effPriority, policiesQ.isError ? null : defaultPolicy)}</p>}
                  </div>
                )}
                {s.escalation_mode === 'policy' && (
                  <div className="space-y-2 rounded-lg border border-border bg-surface2/40 px-4 py-3">
                    <select
                      aria-label="Escalation policy"
                      value={s.escalation_policy_id}
                      onChange={(e) => setS({ ...s, escalation_policy_id: e.target.value })}
                      className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm"
                    >
                      <option value="">{policiesQ.isLoading ? 'Loading…' : policies.length ? 'Choose a policy…' : 'No policies exist yet'}</option>
                      {policies.map((p) => <option key={p.id} value={p.id}>{p.name}{p.enabled ? '' : ' (disabled)'}</option>)}
                    </select>
                    {chosenPolicy && (
                      <p className="text-xs text-muted">
                        {stepSummary(chosenPolicy.steps)}
                        {chosenPolicy.ack_target_minutes ? ` · acknowledge within ${fmtMinutes(chosenPolicy.ack_target_minutes)}` : ''}
                        {chosenPolicy.review_minutes ? ` · updates every ${fmtMinutes(chosenPolicy.review_minutes)}` : ''}
                      </p>
                    )}
                    {chosenPolicy && !chosenPolicy.enabled && <p className="text-xs text-warning">{disabledPolicyText(effPriority, defaultPolicy)}</p>}
                    {s.escalation_levels.length > 0 && <p className="text-xs text-muted">{tiersRemovedText(s.escalation_levels.length, 'policy', effPriority, defaultPolicy)}</p>}
                    {policiesQ.isError && <p className="text-xs text-danger">Could not load policies: {apiErrorMessage(policiesQ.error)}</p>}
                  </div>
                )}
                {s.escalation_mode === 'legacy' && (<>
                <p className="text-xs text-muted">
                  {policiesQ.isLoading ? 'Loading policies…' : policiesQ.isError
                    ? `These tiers take precedence over any ${effPriority} default policy (policies could not be loaded: ${apiErrorMessage(policiesQ.error)}).`
                    : legacyTiersText(s.escalation_levels.length, effPriority, defaultPolicy)}
                </p>
                <div className="rounded-lg border border-border bg-surface2/40 px-4 py-3 text-xs">
                  <span className="font-semibold text-text">Immediately on trigger:</span>{' '}
                  <span className="text-muted">
                    {s.notify_channels.length
                      ? `${s.notify_channels.length} trigger action channel${s.notify_channels.length > 1 ? 's' : ''} (previous step)`
                      : 'no channels selected on the Trigger Actions step'}
                  </span>
                </div>
                {s.escalation_levels.map((lv, i) => (
                  <div key={i} className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-4">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-primary">
                        Escalation level {i + 1}
                      </span>
                      <Button type="button" variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                        aria-label={`Remove escalation level ${i + 1}`} onClick={() => removeEscLevel(i)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-muted">If still unacknowledged after</span>
                      <Input type="number" min={1} className="w-24" value={lv.after_minutes} aria-label={`Escalation level ${i + 1} delay in minutes`}
                        onChange={(e) => updateEsc(i, { after_minutes: Number(e.target.value) })} />
                      <span className="text-muted">minutes, notify:</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {channels.length === 0 ? (
                        <p className="col-span-2 text-xs text-muted">
                          No channels — <a href="/channels" className="text-primary hover:underline">create one</a>
                        </p>
                      ) : channels.map((c) => {
                        const checked = lv.notify_channels.includes(c.id)
                        return (
                          <button key={c.id} type="button" onClick={() => toggleEscChannel(i, c.id)}
                            className={cn('flex items-center justify-between rounded-lg border px-3 py-2 text-left text-xs', checked ? 'border-primary bg-primary/10' : 'border-border')}>
                            <span><span className="font-medium">{c.name}</span> <span className="text-muted">{CHANNEL_TYPE_LABEL[c.type]}</span></span>
                            {checked && <Check className="h-3.5 w-3.5 text-primary" />}
                          </button>
                        )
                      })}
                    </div>
                    <div className="flex items-center gap-2 text-xs">
                      <Switch
                        checked={!!lv.repeat_every_minutes}
                        onCheckedChange={(v) => updateEsc(i, { repeat_every_minutes: v ? Math.max(lv.after_minutes, 15) : null })}
                        aria-label={`Repeat escalation level ${i + 1}`}
                      />
                      <span className="text-muted">Repeat this level every</span>
                      <Input type="number" min={1} className="w-20" disabled={!lv.repeat_every_minutes}
                        aria-label={`Repeat escalation level ${i + 1} every N minutes`}
                        value={lv.repeat_every_minutes ?? ''}
                        onChange={(e) => updateEsc(i, { repeat_every_minutes: Number(e.target.value) || null })} />
                      <span className="text-muted">minutes until acknowledged or resolved</span>
                    </div>
                  </div>
                ))}
                {s.escalation_levels.length < MAX_ESC_LEVELS && (
                  <Button type="button" variant="outline" size="sm" onClick={addEscLevel}>
                    <Plus className="h-3.5 w-3.5" />Add escalation level
                  </Button>
                )}
                <p className="text-xs text-muted">
                  Escalation emails and SMS state the level, how long the alert has been active, and
                  that it is unacknowledged — e.g.&nbsp;
                  <span className="font-mono text-text">[CRITICAL] Escalation L2: Core router down — core-router-01</span>.
                  &quot;Max escalation repeats&quot; on the Trigger Actions step caps repeats.
                </p>
                </>)}
              </div>
            )}

            {step === 'reset_actions' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <SectionTitle title="Reset Actions" hint={isLifecycle ? 'Optional notification when the alert closes after the auto-clear time.' : 'Notifications when the alert clears (recovery).'} />
                  {!isServer && (
                    <Button type="button" variant="outline" size="sm" onClick={copyTriggerToReset}>
                      <Copy className="h-3.5 w-3.5" />Copy from trigger
                    </Button>
                  )}
                </div>
                {isServer ? (
                  <p className="rounded-lg border border-border bg-surface2/40 p-3 text-xs text-muted">
                    {isLifecycle
                      ? s.reset_mode === 'auto'
                        ? 'The configured routes receive a notice when the alert closes. It says whether the server is reporting normally again or its agent is still silent; it never guesses.'
                        : 'Closing notices are off. Select "Notify when the alert closes" on the Reset step to enable them.'
                      : s.reset_mode === 'auto'
                        ? 'When the condition clears, the configured routes receive a recovery notice in the built-in wording, stating how long the condition lasted.'
                        : 'Recovery notices are off. Select "Send a recovery notification" on the Reset step to enable them.'}
                  </p>
                ) : !hasResetTemplates ? (
                  <p className="rounded-lg border border-border bg-surface2 p-3 text-xs text-muted">
                    An SNMP trap is an event, not a state — nothing ever clears it, so there is no
                    reset notification to word.
                  </p>
                ) : s.reset_mode !== 'auto' ? (
                  <p className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
                    Recovery notifications are disabled. Select &quot;Send a recovery notification&quot; on the Reset step.
                  </p>
                ) : (
                  <>
                    <TemplateVars vars={templateVars} />
                    <p className="text-xs text-muted">
                      The wording sent when the condition clears. The built-in text states how long the
                      condition was active &mdash; <span className="text-text">{'{duration_sentence}'}</span>{' '}
                      renders as &ldquo;The condition was active for 12 minutes.&rdquo;
                    </p>
                    <TemplateField label="Recovery email subject" field="recovery_email_subject"
                      value={s.recovery_email_subject} defaults={tplDefaults} onChange={setTemplate} />
                    <TemplateField label="Recovery email body" field="recovery_email_body" rows={4}
                      value={s.recovery_email_body} defaults={tplDefaults} onChange={setTemplate} />
                    <TemplateField label="Recovery SMS" field="recovery_sms_template" rows={2}
                      value={s.recovery_sms_template} defaults={tplDefaults} onChange={setTemplate} />
                    <LiveMail msg={livePreview?.recovery} label="Reset email" accent />
                  </>
                )}
              </div>
            )}

            {step === 'schedule' && (
              <div className="space-y-4">
                <SectionTitle title="Schedule" hint="Limit when notifications are sent (quiet hours). Alerts are still recorded 24/7." />
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Notify from">
                    <Input type="time" value={s.schedule_start} onChange={(e) => setS({ ...s, schedule_start: e.target.value })} />
                  </FormField>
                  <FormField label="Notify until">
                    <Input type="time" value={s.schedule_end} onChange={(e) => setS({ ...s, schedule_end: e.target.value })} />
                  </FormField>
                </div>
                <div>
                  <div className="mb-1.5 text-xs font-medium text-muted">Active days</div>
                  <div className="flex flex-wrap gap-1.5">
                    {DAY_LABELS.map((d) => (
                      <button key={d.n} type="button" onClick={() => toggleDay(d.n)}
                        className={cn('h-8 w-11 rounded-md text-xs font-medium', s.schedule_days.includes(d.n) ? 'bg-primary text-white' : 'bg-surface2 text-muted')}>
                        {d.label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-2 text-xs text-muted">Leave empty for 24/7 notifications. Recovery alerts always send.</p>
                </div>
              </div>
            )}

            {step === 'review' && (
              <div className="space-y-4">
                <SectionTitle title="Review" hint="Confirm settings before saving." />
                <ReviewRow label="Name" value={s.name || '—'} />
                <ReviewRow label="Severity" value={<Badge variant={s.severity === 'critical' ? 'danger' : s.severity === 'warning' ? 'warning' : 'info'}>{s.severity}</Badge>} />
                <ReviewRow label="Trigger" value={summary.condText} />
                {s.source === 'device' && templateScopeApplies(s.conditions) && s.profile_id && (
                  <ReviewRow label="Template" value={templates.find((t) => t.id === s.profile_id)?.name || 'One template'} />
                )}
                {s.source === 'device' && interfaceRoleApplies(s.conditions) && s.if_role && (
                  <ReviewRow label="Interface role" value={ROLE_LABEL[s.if_role] || s.if_role} />
                )}
                {s.source === 'netflow' ? (
                  <>
                    <ReviewRow label="Scope" value={describeScopeDraft(s.nf)} />
                    <ReviewRow label="Noise control" value={`Raise after ${s.nf.hold}, resolve after ${s.nf.clear_hold} evaluation(s)${Number(s.nf.flap_max) ? `; hold open after ${s.nf.flap_max} changes in ${s.nf.flap_window_min} min` : ''}`} />
                  </>
                ) : isServer ? (
                  <>
                    <ReviewRow label="Scope" value={serverScopeLabel(s, {
                      server: (() => { const x = servers.find((v: any) => v.id === s.server_id); return x ? (x.display_name || x.hostname) : null })(),
                      group: deviceGroups.find(({ group: g }) => g.id === s.group_id)?.group.name,
                    })} />
                    {(serverKind === 'pct' || serverKind === 'auto') && <ReviewRow label="Hold time" value={s.min_duration ? `${s.min_duration}s` : 'Immediate'} />}
                  </>
                ) : null}
                {isServer && isLifecycle && (
                  <ReviewRow label="Auto-clear after" value={`${holdLabel(s.clear_duration)} after the last event${s.clear_duration == null ? ' (default)' : ''}`} />
                )}
                {isServer && (serverKind === 'pct' || serverKind === 'auto') && (
                  <ReviewRow label="Clear after" value={s.clear_duration === 0 ? 'First healthy reading' : `${s.clear_duration ?? (isEdit ? DEFAULT_CLEAR_SECONDS : NEW_PERCENT_CLEAR_S)}s${s.clear_duration == null ? ' (default)' : ''}`} />
                )}
                {!isServer && s.source !== 'netflow' && (
                  // What the evaluator honours for this rule, as the rule list shows it: hold, window, clear or auto-clear.
                  <ReviewRow label="Timers" value={timersText(buildPayload(s))} />
                )}
                {flapHold && <ReviewRow label="While flapping" value={s.notify_while_flapping ? 'Notify immediately' : 'Held until a status lasts the flap stability time'} />}
                <ReviewRow label="Reset" value={eventOnly ? (first0?.metric === 'netpath_path_change' ? 'Resolved by hand' : expiry.enabled ? `Expires after ${expiryWindowLabel(expiry.hours)} without a new event` : 'Resolved by hand (event expiry is off)')
                  : isLifecycle || rebootRule ? `Auto-close ${holdLabel(s.clear_duration)} after the last ${rebootRule ? 'reboot' : 'event'}; ${s.reset_mode === 'auto' ? 'notify when it closes' : 'no closing notification'}`
                  : s.reset_mode === 'auto' ? 'Auto-resolve + recovery notification' : 'Auto-resolve without recovery notification'} />
                <ReviewRow label="Channels" value={s.notify_channels.length ? `${s.notify_channels.length} selected` : 'None selected (escalation may still notify)'} />
                <ReviewRow label="Priority" value={<span className="inline-flex items-center gap-1.5"><PriorityBadge priority={effPriority} />{s.priority ? '' : <span className="text-xs text-muted">auto from severity</span>}</span>} />
                <ReviewRow
                  label="Escalation"
                  value={s.escalation_mode === 'policy' && !s.escalation_policy_id
                    ? 'Policy not chosen'
                    : policiesQ.isError && !(s.escalation_mode === 'legacy' && s.escalation_levels.length)
                      ? `Unknown (policies could not be loaded)`
                      : ruleEscalationSummary({
                          // What is saved: a policy only in policy mode, tiers only in legacy mode.
                          policy: s.escalation_mode === 'policy' ? chosenPolicy : null,
                          policyId: s.escalation_mode === 'policy' ? s.escalation_policy_id : null,
                          tiers: s.escalation_mode === 'legacy' ? s.escalation_levels.map((lv) => lv.after_minutes) : [],
                          priority: effPriority,
                          defaultPolicy,
                        })}
                />
                <ReviewRow label="Enabled" value={s.enabled ? 'Yes' : 'No'} />
                {!isServer && <div className="flex gap-2 pt-2">
                  <Button type="button" variant="outline" size="sm" onClick={() => previewMut.mutate()} disabled={previewMut.isPending}>
                    {previewMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eye className="h-3.5 w-3.5" />}
                    Preview message
                  </Button>
                  {isEdit && (
                    <Button type="button" variant="outline" size="sm" onClick={() => simulateMut.mutate()} disabled={simulateMut.isPending}>
                      {simulateMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                      Send test
                    </Button>
                  )}
                </div>}
                {preview && !isServer && (
                  <div className="space-y-3">
                    <EmailPreviewCard title="Alert email" msg={preview.alert} />
                    {preview.recovery && (
                      <EmailPreviewCard title="Recovery email" msg={preview.recovery} accent />
                    )}
                    <p className="text-xs text-muted">
                      Sample device and readings. Use the preview button on the alert list to edit
                      these templates against a live preview.
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Footer */}
          <AlertWizardFooter stepIndex={stepIdx} stepCount={STEPS.length} editing={isEdit} pending={save.isPending}
            onPrevious={() => stepIdx > 0 ? setStep(STEPS[stepIdx - 1].id) : onOpenChange(false)}
            onNext={() => setStep(STEPS[stepIdx + 1].id)} />
        </form>
      </div>
    </div>
  )
}

/** A device rule that, left unscoped, also pages for agent servers (the Device Down rules). */
function downCoversServers(s: WizardState): boolean {
  const first = s.conditions[0]
  return s.source === 'device' && downRuleCoversServers({
    metric: first?.metric, trigger_on: s.trigger_on,
    conditions: s.conditions.length > 1 ? s.conditions : null, operator: first?.operator, threshold: first?.threshold,
  })
}

function SectionTitle({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="mb-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  )
}

function ReviewRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/50 py-2 text-sm">
      <span className="text-muted">{label}</span>
      <span className="max-w-[65%] text-right font-medium">{value}</span>
    </div>
  )
}

function EmailPreviewCard({ title, msg, accent }: { title: string; msg: any; accent?: boolean }) {
  if (!msg) return null
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="border-b border-border bg-surface2/40 px-4 py-2.5">
        <div className={cn('text-xs font-semibold', accent ? 'text-emerald-600' : 'text-text')}>{title}</div>
        <div className="mt-0.5 break-words text-xs text-muted">Subject: {msg.subject}</div>
      </div>
      {/* The real HTML mail, sandboxed — scripts off, no navigation out. */}
      {msg.email_html ? (
        <iframe title={title} sandbox="" srcDoc={msg.email_html} className="h-72 w-full bg-white" />
      ) : (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap p-4 text-xs text-muted">{msg.email_body}</pre>
      )}
    </div>
  )
}

/**
 * The mail as it will arrive, rendered live while the wording is edited.
 *
 * What the fields above control is the one sentence in the highlighted
 * callout and the subject line. Everything framing it — the branded header,
 * the severity pill, the readings block, the details table, the "View in
 * ZenPlus" button and the footer — is built by the email layout and is not
 * template text, which is exactly the thing an editor full of
 * "{event_sentence}" boxes cannot convey.
 */
function LiveMail({ msg, label, accent }: { msg: any; label: string; accent?: boolean }) {
  const [open, setOpen] = useState(true)
  if (!msg) {
    return (
      <div className="rounded-lg border border-border bg-surface2/40 px-4 py-3 text-xs text-muted">
        Rendering {label.toLowerCase()}…
      </div>
    )
  }
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <button type="button" onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 border-b border-border bg-surface2/40 px-4 py-2.5 text-left">
        <div className="min-w-0">
          <div className={cn('text-xs font-semibold', accent ? 'text-emerald-600' : 'text-text')}>
            {label} — as it will arrive
          </div>
          <div className="mt-0.5 truncate text-xs text-muted">Subject: {msg.subject}</div>
        </div>
        <ChevronRight className={cn('h-4 w-4 shrink-0 text-muted transition-transform', open && 'rotate-90')} />
      </button>
      {open && (
        <>
          {/* The real HTML mail, sandboxed — scripts off, no navigation out. */}
          <iframe title={label} sandbox="" srcDoc={msg.email_html} className="h-[26rem] w-full bg-white" />
          <div className="border-t border-border bg-surface2/40 px-4 py-2">
            <div className="text-2xs uppercase tracking-wider text-muted">SMS</div>
            <div className="mt-1 whitespace-pre-wrap text-xs">{msg.sms_body}</div>
            <p className="mt-2 text-xs text-muted">
              Sample device and readings. The header, severity pill, readings, details table and
              button are added by the layout — the fields above set the subject and the highlighted
              sentence.
            </p>
          </div>
        </>
      )}
    </div>
  )
}

/**
 * One message template, shown as the real text that will be sent.
 *
 * The field is never blank: it carries the built-in wording until someone
 * edits it, which is the whole point — an empty box with a grey placeholder
 * told an operator nothing about what their alerts actually say, and gave them
 * nothing to edit but a blank line. "Default"/"Custom" says which state it is
 * in, and "Reset to default" puts the built-in text back (which is stored as
 * NULL, so the rule tracks later improvements to the wording).
 */
function TemplateField({
  label,
  field,
  value,
  defaults,
  onChange,
  rows,
  hint,
}: {
  label: string
  field: TemplateField
  value: string
  defaults: TemplateSet
  onChange: (field: TemplateField, value: string) => void
  rows?: number
  hint?: string
}) {
  const fallback = (defaults[field] as string) || ''
  const isCustom = value.trim() !== fallback.trim()
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <label className="text-xs font-medium text-muted">{label}</label>
        <div className="flex items-center gap-2">
          <span className={cn('text-2xs uppercase tracking-wider', isCustom ? 'text-primary' : 'text-muted')}>
            {isCustom ? 'Custom' : 'Default'}
          </span>
          {isCustom && (
            <button type="button" className="text-2xs text-muted underline-offset-2 hover:text-primary hover:underline"
              onClick={() => onChange(field, fallback)}>
              Reset to default
            </button>
          )}
        </div>
      </div>
      {rows ? (
        <Textarea value={value} rows={rows} className="font-mono text-xs"
          onChange={(e) => onChange(field, e.target.value)} />
      ) : (
        <Input value={value} className="font-mono text-xs"
          onChange={(e) => onChange(field, e.target.value)} />
      )}
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  )
}

/** The placeholders this rule's template kind accepts (anything else is refused on save). */
function TemplateVars({ vars }: { vars: string[] }) {
  return (
    <div className="rounded-lg border border-primary/20 bg-primary/5 p-3">
      <p className="text-2xs font-semibold uppercase tracking-wider text-primary">Template variables for this rule</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {vars.map((v) => (
          <code key={v} className="cursor-pointer rounded bg-surface px-1.5 py-0.5 text-2xs text-muted hover:text-primary"
            onClick={() => { void copyText(v) }} title="Click to copy">{v}</code>
        ))}
      </div>
    </div>
  )
}

type WizardTemplateMetric = { key: string; name: string; unit?: string | null; type?: string | null; thresholds?: { warn?: number | null; crit?: number | null; op?: string } | null; labels?: Record<string, any> | null; value_map?: Record<string, any> | null }
type WizardTemplate = { id: string; name: string; oid_groups?: { key: string; name: string; display?: string | null; metrics?: WizardTemplateMetric[] }[] }

/** key -> metric name over every template, for template metric rules (tpl_<key>). */
function templateMetricNames(templates: WizardTemplate[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const t of templates) for (const g of t.oid_groups || []) for (const m of g.metrics || []) if (!out[m.key]) out[m.key] = m.name
  return out
}

/** Whether a template metric is a state (enum) metric with a value map, which a rule picks states of. */
function isStateMetric(m: WizardTemplateMetric): boolean {
  return m.type === 'enum' && (Object.keys(m.labels || {}).length > 0 || Object.keys(m.value_map || {}).length > 0)
}

/** A template's gauge metrics (compared with a threshold) and state metrics (alert on chosen states). */
function gaugeMetrics(t: WizardTemplate | undefined): { group: string; metric: WizardTemplateMetric }[] {
  const seen = new Set<string>()
  return (t?.oid_groups || []).filter((g) => g.display !== 'children').flatMap((g) => (g.metrics || [])
    .filter((m) => (!m.type || m.type === 'gauge' || isStateMetric(m)) && !seen.has(m.key) && !!seen.add(m.key))
    .map((metric) => ({ group: g.name, metric })))
}

/** Template metric picker: a template, then one of its gauge metrics (the rule metric becomes tpl_<key>). */
function TemplateMetricPicker({ metric, templates, scopeTemplateId, onPick, className }: {
  metric: string
  templates: WizardTemplate[]
  scopeTemplateId: string
  onPick: (m: WizardTemplateMetric, templateId: string) => void
  className?: string
}) {
  const key = metric.startsWith('tpl_') ? metric.slice(4) : ''
  const has = (t: WizardTemplate) => !!key && gaugeMetrics(t).some((x) => x.metric.key === key)
  const initial = templates.find((t) => t.id === scopeTemplateId && (!key || has(t)))?.id
    || templates.find(has)?.id || scopeTemplateId || ''
  const [tplId, setTplId] = useState(initial)
  // The template list may arrive after the picker opened (editing a saved rule).
  useEffect(() => { if (!tplId && initial) setTplId(initial) }, [initial, tplId])
  const chosen = templates.find((t) => t.id === tplId)
  const options = gaugeMetrics(chosen)
  return (
    <div className={cn('grid gap-2 rounded-md border border-dashed border-border p-2 sm:grid-cols-2', className)}>
      <FormField label="Template">
        <Select value={tplId || '__none__'} onValueChange={(v) => setTplId(v === '__none__' ? '' : v)}>
          <SelectTrigger><SelectValue placeholder="Choose a template" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__" disabled>Choose a template</SelectItem>
            {templates.map((t) => <SelectItem key={t.id} value={t.id}>{t.name}</SelectItem>)}
          </SelectContent>
        </Select>
      </FormField>
      <FormField label="Metric" hint={chosen && !options.length ? 'This template has no gauge or state metric.' : 'A gauge starts at the template’s critical level, when it has one; a state metric alerts on the states you tick.'}>
        <Select value={key && options.some((x) => x.metric.key === key) ? key : '__none__'}
          onValueChange={(v) => { const m = options.find((x) => x.metric.key === v)?.metric; if (m && chosen) onPick(m, chosen.id) }}>
          <SelectTrigger><SelectValue placeholder="Choose a metric" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__" disabled>{key ? `${key} (not in this template)` : 'Choose a metric'}</SelectItem>
            {options.map(({ group, metric: m }) => (
              <SelectItem key={`${group}:${m.key}`} value={m.key}>{group}: {m.name}{isStateMetric(m) ? ' (state)' : m.unit ? ` (${m.unit})` : ''}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </FormField>
    </div>
  )
}
