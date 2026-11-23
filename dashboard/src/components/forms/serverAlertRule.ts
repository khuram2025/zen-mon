/**
 * Server (agent) rules in the alert wizard and the Alert Manager list.
 *
 * Server rules are the host_* metrics evaluated by host_alert_service. They are
 * stored in alert_rules like every other rule but saved through
 * /host-alert-rules, which accepts a server scope and none of the device
 * columns. The server's own Alerts tab writes the same rules.
 */
import { OP_SYMBOL, isServerMetric, isServerPercent, serverMetric, serverMetricKind, serverMetricLabel, serverOperators, type ServerMetricKind } from './serverMetrics'
import {
  COMBINABLE_SERVER_METRICS, conditionTreePayload, newCondition, treeConditions, treeLabel, treeProblem,
  type ConditionTree, type TreeCondition, type TreeGroup,
} from './conditionTree'

export * from './serverMetrics'
export { COMBINABLE_SERVER_METRICS, MAX_TREE_CONDITIONS, TREE_INSTANCE, isTreeGroup, treeConditions } from './conditionTree'

/** Heartbeat silence before an Agent offline rule fires when it sets none (server_availability.DOWN_AFTER_S). */
export const DEFAULT_SILENCE_SECONDS = 180
/** The shortest silence a rule may set (server_availability.MIN_DOWN_AFTER_S). */
export const MIN_SILENCE_SECONDS = 90

/** One line describing when the rule fires, for the rule list and the Review step. */
export function serverRuleCondition(r: { metric: string; operator?: string | null; threshold?: number | null; target?: string | null; min_duration?: number | null; condition_tree?: ServerTree | null }): string {
  if (r.condition_tree && Array.isArray(r.condition_tree.items)) return serverTreeLabel(r.condition_tree)
  const kind = serverMetricKind(r.metric)
  if (kind === 'svc') return `Service ${r.target || '?'} stopped`
  if (kind === 'proc') return `Process ${r.target || '?'} not running`
  if (kind === 'event') return serverMetricLabel(r.metric)
  if (kind === 'auto') {
    const left = String(r.target || '').trim()
    return left ? `Automatic service stopped (except ${left})` : 'Automatic service stopped'
  }
  if (kind === 'flag') {
    const after = Math.max(Number(r.min_duration) || DEFAULT_SILENCE_SECONDS, MIN_SILENCE_SECONDS)
    return `Agent silent for ${after >= 120 ? `${Math.round(after / 60)} min` : `${after}s`}, server still answers ping`
  }
  const op = OP_SYMBOL[String(r.operator || '')] || r.operator || '>'
  const spec = serverMetric(r.metric)
  // An unknown (newer) metric shows its value without a unit rather than as a percentage.
  const label = spec ? spec.label.replace(/ \([^)]*\)$/, '') : r.metric
  return `${label} ${op} ${r.threshold ?? 0}${spec?.unit ?? ''}`
}

/* ── Combined rules (condition_tree, migrate-211/212; the shared model is conditionTree.ts) ──────────── */

export type ServerTreeCondition = TreeCondition
export type ServerTreeGroup = TreeGroup
export type ServerTree = ConditionTree

/** A new server condition on a metric, with that metric's starting limit. */
export function newTreeCondition(metric = 'host_memory_pct'): ServerTreeCondition {
  return newCondition('server', metric)
}

/** The draft's single condition plus a second one, as a combined rule ("Add condition"). */
export function treeFromDraft(s: Pick<ServerDraft, 'conditions' | 'target'>): ServerTree {
  const c = s.conditions[0]
  const first: ServerTreeCondition = c && COMBINABLE_SERVER_METRICS.some((m) => m.value === c.metric)
    ? { metric: c.metric, operator: c.operator, threshold: Number(c.threshold), reset_threshold: c.reset_threshold ?? null, target: s.target || '' }
    : newTreeCondition('host_cpu_pct')
  const second = newTreeCondition(first.metric === 'host_memory_pct' ? 'host_cpu_pct' : 'host_memory_pct')
  return { logic: 'AND', items: [first, second] }
}

/** The single condition a combined rule falls back to ("Use one condition"): its first on the server itself. */
export function singleFromTree(tree: ServerTree): { conditions: ServerDraft['conditions']; target: string } {
  const c = treeConditions(tree).find((x) => !x.on || x.on === 'self') || newTreeCondition('host_cpu_pct')
  return { conditions: [{ metric: c.metric, operator: c.operator, threshold: Number(c.threshold), reset_threshold: c.reset_threshold ?? null }], target: c.target || '' }
}

/** "(CPU usage > 90% and Memory usage > 90%) or Service nginx stopped" for the list and the Review step. */
export function serverTreeLabel(tree: ServerTree): string {
  return treeLabel('server', tree)
}

/** What stops a combined server rule from being saved, or null (mirrors rule_conditions.validate_tree). */
export function serverTreeProblem(tree: ServerTree): string | null {
  return treeProblem('server', tree)
}

/** The tree as /host-alert-rules stores it. */
export function treePayload(tree: ServerTree): ServerTree {
  return conditionTreePayload('server', tree)
}

/**
 * The automatic clear level (host_alert_rules._auto_reset): 2 points on the healthy side for percent rules,
 * one day above the limit for days-until-full, none for the other threshold metrics. Without a metric the
 * percent rule applies.
 */
export function serverAutoReset(operator: string, threshold: number, metric?: string): number | null {
  const op = OP_SYMBOL[operator] || operator
  if (!Number.isFinite(threshold)) return null
  if (metric === 'host_fs_days_left') return op === '<' || op === '<=' ? threshold + 1 : null
  if (metric && !isServerPercent(metric)) return null
  if ((op === '>' || op === '>=') && threshold - 2 >= 0) return threshold - 2
  if ((op === '<' || op === '<=') && threshold + 2 <= 100) return threshold + 2
  return null
}

const PING_CMP: Record<string, (v: number, t: number) => boolean> = {
  eq: (v, t) => v === t, '==': (v, t) => v === t, neq: (v, t) => v !== t, '!=': (v, t) => v !== t,
  lt: (v, t) => v < t, '<': (v, t) => v < t, lte: (v, t) => v <= t, '<=': (v, t) => v <= t,
  gt: (v, t) => v > t, '>': (v, t) => v > t, gte: (v, t) => v >= t, '>=': (v, t) => v >= t,
}

function firesOnDownOnly(operator: unknown, threshold: unknown): boolean {
  const cmp = PING_CMP[String(operator || '').trim().toLowerCase()]
  const t = Number(threshold)
  return !!cmp && Number.isFinite(t) && cmp(0, t) && !cmp(1, t)
}

/**
 * Whether a ping-status rule fires on "goes down", so that, left unscoped, it also pages for agent servers. Mirrors
 * server_availability.is_down_rule: trigger "down", or an "any" rule whose only conditions are ping_status == 0 (the
 * seeded Device Down). The device/group/type/location scope is the caller's to check.
 */
export function downRuleCoversServers(r: { metric?: string; trigger_on?: string | null; conditions?: any[] | null; operator?: unknown; threshold?: unknown }): boolean {
  if (r.metric !== 'ping_status') return false
  const trigger = String(r.trigger_on || 'any').toLowerCase()
  if (trigger === 'down') return true
  if (trigger !== 'any') return false
  const conds = Array.isArray(r.conditions) ? r.conditions : []
  if (conds.length) return conds.every((c: any) => c?.metric === 'ping_status' && firesOnDownOnly(c.operator, c.threshold))
  return firesOnDownOnly(r.operator, r.threshold)
}

/**
 * Which servers a rule covers: every server, one server, the servers carrying a tag, or the members of a device group
 * (static or dynamic, subgroups included). A tag and a group may both be set: a server must then match both.
 */
export type ServerScopeMode = 'all' | 'one' | 'tag' | 'group'

export const SERVER_SCOPE_OPTIONS: ReadonlyArray<{ value: ServerScopeMode; label: string }> = [
  { value: 'all', label: 'All servers' },
  { value: 'one', label: 'One server' },
  { value: 'tag', label: 'Servers with a tag' },
  { value: 'group', label: 'Servers in a group' },
]

/** The longest tag a rule may be scoped to (host_alert_rules.HostRuleCreate.scope_tag). */
export const MAX_SCOPE_TAG = 64

type ScopeFields = { server_scope?: ServerScopeMode | null; server_id?: string | null; scope_tag?: string | null; group_id?: string | null }

/**
 * The scope a draft or saved rule uses. A server id (or the '__pick__' placeholder) always means one server; otherwise
 * the wizard's choice, or without one the fields it implies.
 */
export function serverScopeOf(s: ScopeFields): ServerScopeMode {
  if (s.server_id) return 'one'
  if (s.server_scope && s.server_scope !== 'one') return s.server_scope
  if (s.group_id) return 'group'
  if (s.scope_tag) return 'tag'
  return 'all'
}

/** The draft fields after choosing a scope. A tag and a group survive switching between the two; one server drops them. */
export function serverScopeChange(mode: ServerScopeMode, s: ScopeFields): { server_scope: ServerScopeMode; server_id: string; scope_tag: string; group_id: string } {
  if (mode === 'one') return { server_scope: mode, server_id: '__pick__', scope_tag: '', group_id: '' }
  if (mode === 'all') return { server_scope: mode, server_id: '', scope_tag: '', group_id: '' }
  return { server_scope: mode, server_id: '', scope_tag: s.scope_tag || '', group_id: s.group_id || '' }
}

/**
 * The rule list's words for a server rule's scope: "All servers", the server's name, "Servers with tag prod",
 * "Servers in SQL hosts", "Servers in SQL hosts with tag prod".
 */
export function serverScopeLabel(r: ScopeFields, names: { server?: string | null; group?: string | null } = {}): string {
  if (r.server_id) return names.server || 'One server'
  const tag = String(r.scope_tag || '').trim()
  const group = r.group_id ? names.group || 'one device group' : ''
  if (group && tag) return `Servers in ${group} with tag ${tag}`
  if (group) return `Servers in ${group}`
  if (tag) return `Servers with tag ${tag}`
  return 'All servers'
}

/** The wizard fields a server rule is built from. */
export type ServerDraft = {
  name: string
  description: string
  enabled: boolean
  severity: string
  priority: string
  escalation_mode: string
  escalation_policy_id: string
  conditions: { metric: string; operator: string; threshold: number; reset_threshold?: number | null }[]
  target: string
  server_id: string
  /** The scope chosen in the wizard; derived from the fields when absent (see serverScopeOf). */
  server_scope?: ServerScopeMode
  /** Without one server: only servers carrying this tag ('' = any). */
  scope_tag?: string
  /** Without one server: only members of this device group ('' = any). */
  group_id?: string
  min_duration: number
  clear_duration: number | null
  reset_mode: 'auto' | 'none'
  notify_channels: string[]
  cooldown: number
  schedule_start: string
  schedule_end: string
  schedule_days: number[]
  /** A combined rule: two or more conditions joined by AND/OR. null = one condition (`conditions[0]`). */
  condition_tree?: ServerTree | null
}

/** Defaults when changing what a server rule watches; preserve valid zero values between percent metrics. */
export function serverWatchChange(s: Pick<ServerDraft, 'conditions' | 'min_duration' | 'reset_mode'>, metric: string) {
  const current = s.conditions[0]
  const previousKind = serverMetricKind(current?.metric)
  const nextKind = serverMetricKind(metric)
  const resetMode = nextKind === 'event' && previousKind !== 'event' ? 'none' : s.reset_mode
  const next = serverMetric(metric)
  // A limit carries over only between metrics on the same scale (percent to percent).
  const sameScale = previousKind === 'pct' && serverMetric(current?.metric)?.unit === next?.unit
  const ops = serverOperators(metric)
  const prevOp = OP_SYMBOL[current?.operator] || current?.operator
  const held = (k: ServerMetricKind) => k === 'pct' || k === 'auto'
  return {
    target: '',
    min_duration: held(nextKind) ? (held(previousKind) ? s.min_duration : 300) : 0,
    conditions: [{ metric,
      operator: nextKind === 'pct' && previousKind === 'pct' && ops.includes(prevOp) ? prevOp : nextKind === 'pct' ? ops[0] : '>',
      threshold: nextKind === 'pct' ? (sameScale ? current.threshold : next?.defaultThreshold ?? 90) : 0,
      reset_threshold: null }],
    reset_mode: resetMode,
    recovery_alert: resetMode === 'auto',
  }
}

/** What is missing before the draft can be saved, or null. */
export function serverDraftProblem(s: ServerDraft): { step: 'trigger' | 'scope'; message: string } | null {
  if (s.condition_tree) {
    const problem = serverTreeProblem(s.condition_tree)
    if (problem) return { step: 'trigger', message: problem }
    return serverScopeProblem(s)
  }
  const c = s.conditions[0]
  const kind = serverMetricKind(c?.metric)
  if (!c || !isServerMetric(c.metric)) return { step: 'trigger', message: 'Choose what to watch on the server.' }
  if (kind === 'svc' && !s.target.trim()) return { step: 'trigger', message: 'Enter the service name.' }
  if (kind === 'proc' && !s.target.trim()) return { step: 'trigger', message: 'Enter the process name.' }
  const spec = serverMetric(c.metric)
  if (kind === 'pct' && isServerPercent(c.metric) && !(Number(c.threshold) >= 0 && Number(c.threshold) <= 100)) {
    return { step: 'trigger', message: 'The threshold is a percentage from 0 to 100.' }
  }
  if (kind === 'pct' && !Number.isFinite(Number(c.threshold))) return { step: 'trigger', message: 'Enter the limit.' }
  if (kind === 'pct' && spec?.operators && !spec.operators.includes(OP_SYMBOL[c.operator] || c.operator)) {
    return { step: 'trigger', message: `${spec.label}: use ${spec.operators.join(' or ')}.` }
  }
  if (c.metric === 'host_fs_days_left' && !(Number(c.threshold) > 0)) return { step: 'trigger', message: 'Days until full must be more than 0.' }
  if (kind === 'pct' && spec && !isServerPercent(c.metric) && Number(c.threshold) < 0) {
    return { step: 'trigger', message: 'The limit cannot be negative.' }
  }
  return serverScopeProblem(s)
}

function serverScopeProblem(s: ServerDraft): { step: 'trigger' | 'scope'; message: string } | null {
  const scope = serverScopeOf(s)
  if (scope === 'one' && (!s.server_id || s.server_id === '__pick__')) return { step: 'scope', message: 'Select a server, or choose "All servers".' }
  if (scope === 'tag' && !String(s.scope_tag || '').trim()) return { step: 'scope', message: 'Choose a tag, or choose "All servers".' }
  if (scope === 'group' && !s.group_id) return { step: 'scope', message: 'Select a device group, or choose "All servers".' }
  if (String(s.scope_tag || '').trim().length > MAX_SCOPE_TAG) return { step: 'scope', message: `A tag is at most ${MAX_SCOPE_TAG} characters.` }
  return null
}

/** The /host-alert-rules body for the draft (create and update take the same fields). */
export function serverRulePayload(s: ServerDraft): Record<string, unknown> {
  if (s.condition_tree) return combinedRulePayload(s, s.condition_tree)
  const c = s.conditions[0]
  const kind = serverMetricKind(c.metric)
  const pct = kind === 'pct'
  const scope = serverScopeOf(s)
  const filtered = scope === 'tag' || scope === 'group'
  return {
    name: s.name,
    description: s.description || null,
    enabled: s.enabled,
    metric: c.metric,
    operator: pct ? c.operator : 'gt',
    threshold: pct ? Number(c.threshold) : 0,
    severity: s.severity,
    // flag: heartbeat silence before it fires (0 = the default); auto: a breach hold per service;
    // svc/proc fire on the reported state.
    min_duration: pct || kind === 'flag' || kind === 'auto' ? Math.max(0, Math.round(Number(s.min_duration) || 0)) : 0,
    // pct/auto: how long the cleared condition must hold; event: how long the
    // restart/shutdown alert stays open after the last event (auto-clear).
    clear_duration: pct || kind === 'event' || kind === 'auto' ? s.clear_duration : null,
    reset_threshold: pct && c.reset_threshold != null ? Number(c.reset_threshold) : null,
    recovery_alert: s.reset_mode === 'auto',
    cooldown: s.cooldown,
    notify_channels: s.notify_channels,
    server_id: scope === 'one' && s.server_id && s.server_id !== '__pick__' ? s.server_id : null,
    // Sent on every save, so an edit that returns to all servers or one server clears them.
    scope_tag: filtered ? String(s.scope_tag || '').trim() || null : null,
    group_id: filtered ? s.group_id || null : null,
    // auto: the services to leave out (patterns), optional.
    target: kind === 'svc' || kind === 'proc' ? s.target.trim() : kind === 'auto' ? s.target.trim() || null : null,
    priority: s.priority || null,
    escalation_policy_id: s.escalation_mode === 'policy' ? s.escalation_policy_id || null : null,
    schedule_start: s.schedule_start || null,
    schedule_end: s.schedule_end || null,
    schedule_days: s.schedule_days.length ? s.schedule_days : null,
    // Sent on every save: an edit that goes back to one condition turns a combined rule single.
    condition_tree: null,
  }
}

/** The body of a combined rule: the tree carries every condition; the holds and the rest as for one condition. */
function combinedRulePayload(s: ServerDraft, tree: ServerTree): Record<string, unknown> {
  const scope = serverScopeOf(s)
  const filtered = scope === 'tag' || scope === 'group'
  return {
    name: s.name,
    description: s.description || null,
    enabled: s.enabled,
    condition_tree: treePayload(tree),
    severity: s.severity,
    min_duration: Math.max(0, Math.round(Number(s.min_duration) || 0)),
    clear_duration: s.clear_duration,
    recovery_alert: s.reset_mode === 'auto',
    cooldown: s.cooldown,
    notify_channels: s.notify_channels,
    server_id: scope === 'one' && s.server_id && s.server_id !== '__pick__' ? s.server_id : null,
    scope_tag: filtered ? String(s.scope_tag || '').trim() || null : null,
    group_id: filtered ? s.group_id || null : null,
    priority: s.priority || null,
    escalation_policy_id: s.escalation_mode === 'policy' ? s.escalation_policy_id || null : null,
    schedule_start: s.schedule_start || null,
    schedule_end: s.schedule_end || null,
    schedule_days: s.schedule_days.length ? s.schedule_days : null,
  }
}

/** A disabled copy of a saved server rule, for Duplicate on the rule list. */
export function serverRuleCopy(r: any): Record<string, unknown> {
  const reset = Array.isArray(r.conditions)
    ? r.conditions.find((c: any) => c?.metric === r.metric && c?.reset_threshold != null)?.reset_threshold ?? null
    : null
  return serverRulePayload({
    name: `${r.name} (copy)`,
    description: r.description || '',
    enabled: false,
    severity: r.severity || 'warning',
    priority: r.priority || '',
    escalation_mode: r.escalation_policy_id ? 'policy' : 'default',
    escalation_policy_id: r.escalation_policy_id || '',
    conditions: [{ metric: r.metric, operator: r.operator || 'gt', threshold: Number(r.threshold ?? 0), reset_threshold: reset }],
    target: r.target || '',
    server_id: r.server_id || '',
    scope_tag: r.server_id ? '' : r.scope_tag || '',
    group_id: r.server_id ? '' : r.group_id || '',
    min_duration: Number(r.min_duration ?? 0),
    clear_duration: typeof r.clear_duration === 'number' ? r.clear_duration : null,
    reset_mode: r.recovery_alert ? 'auto' : 'none',
    notify_channels: Array.isArray(r.notify_channels) ? r.notify_channels : [],
    cooldown: Number(r.cooldown ?? 300),
    schedule_start: r.schedule_start || '',
    schedule_end: r.schedule_end || '',
    schedule_days: Array.isArray(r.schedule_days) ? r.schedule_days : [],
    condition_tree: r.condition_tree && Array.isArray(r.condition_tree.items) ? r.condition_tree : null,
  })
}
