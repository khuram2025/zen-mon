/**
 * Server (agent) rules in the alert wizard and the Alert Manager list.
 *
 * Server rules are the host_* metrics evaluated by host_alert_service. They are
 * stored in alert_rules like every other rule but saved through
 * /host-alert-rules, which accepts a server scope and none of the device
 * columns. The server's own Alerts tab writes the same rules.
 */

/**
 * pct: a reading compared with a limit (percentages and, despite the name, the depth metrics: what the host
 * API calls a threshold metric, with a clear level and a clear hold); svc/proc: a named service or process;
 * flag: agent offline; event: restart/shutdown; auto: an automatic service stopped (no limit; the target
 * lists services to leave out).
 */
export type ServerMetricKind = 'pct' | 'svc' | 'proc' | 'flag' | 'event' | 'auto'

export type ServerMetric = {
  value: string
  label: string
  kind: ServerMetricKind
  /** Appended to the limit: '%', ' ms', ' errors/s', ' s', ' days'. */
  unit?: string
  /** Operators the host API accepts for it (default: all six). */
  operators?: readonly string[]
  /** The limit a new rule starts with. */
  defaultThreshold?: number
  min?: number
  max?: number
  /** Shown under the condition. */
  hint?: string
}

export const SERVER_METRICS: ReadonlyArray<ServerMetric> = [
  { value: 'host_cpu_pct', label: 'CPU usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_memory_pct', label: 'Memory usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_swap_pct', label: 'Swap usage (%)', kind: 'pct', unit: '%', defaultThreshold: 80, min: 0, max: 100,
    hint: 'Swap (page file) in use, averaged over the hold. Servers without swap are not judged.' },
  { value: 'host_filesystem_pct', label: 'Filesystem usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_fs_days_left', label: 'Filesystem full in (days)', kind: 'pct', unit: ' days', operators: ['<', '<='], defaultThreshold: 7, min: 0,
    hint: 'Days until each filesystem is full at its growth over the last 7 days (needs a day of history; a filesystem that is not filling is healthy). Clears one day above the limit unless a reset value is set.' },
  { value: 'host_disk_util_pct', label: 'Disk I/O utilization (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_disk_latency_ms', label: 'Disk latency (ms)', kind: 'pct', unit: ' ms', defaultThreshold: 50, min: 0,
    hint: 'The slower of the average read and write time per I/O, per disk, averaged over the hold. Linux agents only: Windows servers are skipped until the agent reports real per-I/O times.' },
  { value: 'host_nic_errors', label: 'Network interface errors (per second)', kind: 'pct', unit: ' errors/s', defaultThreshold: 10, min: 0,
    hint: 'Receive plus transmit errors per second, per network interface, averaged over the hold.' },
  { value: 'host_clock_skew_s', label: 'Clock offset from the appliance (s)', kind: 'pct', unit: ' s', defaultThreshold: 60, min: 0,
    hint: 'How far the server’s clock is from the appliance’s, measured at every upload; every sample must breach for the hold.' },
  { value: 'host_service_down', label: 'Service stopped', kind: 'svc' },
  { value: 'host_auto_service_stopped', label: 'Automatic service stopped', kind: 'auto',
    hint: 'Any service set to start automatically that stopped unexpectedly (non-zero exit code, or failed), one alert per service. Services that end cleanly (delayed-start, trigger-start, one-shot) are not reported; to watch every stop of one service, use a Service stopped rule.' },
  { value: 'host_process_down', label: 'Process not running', kind: 'proc' },
  { value: 'host_agent_offline', label: 'Agent offline', kind: 'flag' },
  { value: 'host_restart', label: 'Server restart / unexpected shutdown', kind: 'event' },
  { value: 'host_shutdown', label: 'Server shutdown requested / reported', kind: 'event' },
]

export function serverMetric(metric: string | null | undefined): ServerMetric | undefined {
  return SERVER_METRICS.find((m) => m.value === metric)
}

/** The operators a server rule may use for this metric. */
export function serverOperators(metric: string): readonly string[] {
  return serverMetric(metric)?.operators || ['>', '>=', '<', '<=', '==', '!=']
}

/** Whether the metric's limit is a percentage (0-100). */
export function isServerPercent(metric: string): boolean {
  return serverMetric(metric)?.unit === '%'
}

/** Heartbeat silence before an Agent offline rule fires when it sets none (server_availability.DOWN_AFTER_S). */
export const DEFAULT_SILENCE_SECONDS = 180
/** The shortest silence a rule may set (server_availability.MIN_DOWN_AFTER_S). */
export const MIN_SILENCE_SECONDS = 90

const OP_SYMBOL: Record<string, string> = { gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=' }

export function isServerMetric(metric: unknown): boolean {
  return String(metric || '').startsWith('host_')
}

export function serverMetricKind(metric: string): ServerMetricKind {
  return SERVER_METRICS.find((m) => m.value === metric)?.kind || 'pct'
}

export function serverMetricLabel(metric: string): string {
  return serverMetric(metric)?.label || metric
}

/** One line describing when the rule fires, for the rule list and the Review step. */
export function serverRuleCondition(r: { metric: string; operator?: string | null; threshold?: number | null; target?: string | null; min_duration?: number | null }): string {
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
  const scope = serverScopeOf(s)
  if (scope === 'one' && (!s.server_id || s.server_id === '__pick__')) return { step: 'scope', message: 'Select a server, or choose "All servers".' }
  if (scope === 'tag' && !String(s.scope_tag || '').trim()) return { step: 'scope', message: 'Choose a tag, or choose "All servers".' }
  if (scope === 'group' && !s.group_id) return { step: 'scope', message: 'Select a device group, or choose "All servers".' }
  if (String(s.scope_tag || '').trim().length > MAX_SCOPE_TAG) return { step: 'scope', message: `A tag is at most ${MAX_SCOPE_TAG} characters.` }
  return null
}

/** The /host-alert-rules body for the draft (create and update take the same fields). */
export function serverRulePayload(s: ServerDraft): Record<string, unknown> {
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
  })
}
