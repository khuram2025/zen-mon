/**
 * Server (agent) rules in the alert wizard and the Alert Manager list.
 *
 * Server rules are the host_* metrics evaluated by host_alert_service. They are
 * stored in alert_rules like every other rule but saved through
 * /host-alert-rules, which accepts a server scope and none of the device
 * columns. The server's own Alerts tab writes the same rules.
 */

export type ServerMetricKind = 'pct' | 'svc' | 'proc' | 'flag' | 'event'

export const SERVER_METRICS: ReadonlyArray<{ value: string; label: string; kind: ServerMetricKind }> = [
  { value: 'host_cpu_pct', label: 'CPU usage (%)', kind: 'pct' },
  { value: 'host_memory_pct', label: 'Memory usage (%)', kind: 'pct' },
  { value: 'host_filesystem_pct', label: 'Filesystem usage (%)', kind: 'pct' },
  { value: 'host_disk_util_pct', label: 'Disk I/O utilization (%)', kind: 'pct' },
  { value: 'host_service_down', label: 'Service stopped', kind: 'svc' },
  { value: 'host_process_down', label: 'Process not running', kind: 'proc' },
  { value: 'host_agent_offline', label: 'Agent offline', kind: 'flag' },
  { value: 'host_restart', label: 'Server restart / unexpected shutdown', kind: 'event' },
  { value: 'host_shutdown', label: 'Server shutdown requested / reported', kind: 'event' },
]

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
  return SERVER_METRICS.find((m) => m.value === metric)?.label || metric
}

/** One line describing when the rule fires, for the rule list and the Review step. */
export function serverRuleCondition(r: { metric: string; operator?: string | null; threshold?: number | null; target?: string | null; min_duration?: number | null }): string {
  const kind = serverMetricKind(r.metric)
  if (kind === 'svc') return `Service ${r.target || '?'} stopped`
  if (kind === 'proc') return `Process ${r.target || '?'} not running`
  if (kind === 'event') return serverMetricLabel(r.metric)
  if (kind === 'flag') {
    const after = Math.max(Number(r.min_duration) || DEFAULT_SILENCE_SECONDS, MIN_SILENCE_SECONDS)
    return `Agent silent for ${after >= 120 ? `${Math.round(after / 60)} min` : `${after}s`}, server still answers ping`
  }
  const op = OP_SYMBOL[String(r.operator || '')] || r.operator || '>'
  return `${serverMetricLabel(r.metric).replace(' (%)', '')} ${op} ${r.threshold ?? 0}%`
}

/** The automatic clear level for a percent rule: 2 points on the healthy side (host_alert_rules._auto_reset). */
export function serverAutoReset(operator: string, threshold: number): number | null {
  const op = OP_SYMBOL[operator] || operator
  if (!Number.isFinite(threshold)) return null
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
  return {
    target: '',
    min_duration: nextKind === 'pct' ? (previousKind === 'pct' ? s.min_duration : 300) : 0,
    conditions: [{ metric, operator: previousKind === 'pct' ? current.operator : '>',
      threshold: nextKind === 'pct' ? (previousKind === 'pct' ? current.threshold : 90) : 0,
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
  if (kind === 'pct' && !(Number(c.threshold) >= 0 && Number(c.threshold) <= 100)) {
    return { step: 'trigger', message: 'The threshold is a percentage from 0 to 100.' }
  }
  if (s.server_id === '__pick__') return { step: 'scope', message: 'Select a server, or choose "All servers".' }
  return null
}

/** The /host-alert-rules body for the draft (create and update take the same fields). */
export function serverRulePayload(s: ServerDraft): Record<string, unknown> {
  const c = s.conditions[0]
  const kind = serverMetricKind(c.metric)
  const pct = kind === 'pct'
  return {
    name: s.name,
    description: s.description || null,
    enabled: s.enabled,
    metric: c.metric,
    operator: pct ? c.operator : 'gt',
    threshold: pct ? Number(c.threshold) : 0,
    severity: s.severity,
    // flag: heartbeat silence before it fires (0 = the default); svc/proc fire on the reported state.
    min_duration: pct || kind === 'flag' ? Math.max(0, Math.round(Number(s.min_duration) || 0)) : 0,
    clear_duration: pct ? s.clear_duration : null,
    reset_threshold: pct && c.reset_threshold != null ? Number(c.reset_threshold) : null,
    recovery_alert: s.reset_mode === 'auto',
    cooldown: s.cooldown,
    notify_channels: s.notify_channels,
    server_id: s.server_id && s.server_id !== '__pick__' ? s.server_id : null,
    target: kind === 'svc' || kind === 'proc' ? s.target.trim() : null,
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
