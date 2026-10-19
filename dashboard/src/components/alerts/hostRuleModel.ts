/**
 * Server (host_*) alert rules: labels, the create/edit form, its validation and
 * the request each mode sends (work/RULES-COVERAGE-PLAN.md "UI").
 *
 * Server rules live in /host-alert-rules: create = POST /host-alert-rules,
 * edit = PUT /host-alert-rules/{id} with only that endpoint's update fields
 * (HostRuleUpdate — the scope, server_id, cannot change on edit). The generic
 * /alert-rules endpoint refuses host_* metrics, so the generic wizard never
 * edits them. Imports only the pure rule checks, so node tests run it as is.
 */
import { conditionDomainError, normalizeOperator, resetDomainError, type FieldIssue } from '@/components/forms/alertRuleChecks'

export interface HostRule {
  id: string
  name: string
  description?: string | null
  enabled: boolean
  metric: string
  operator: string
  threshold: number | null
  severity: string
  min_duration: number
  /** Seconds the cleared condition must hold before the alert resolves; null = default (300 s). */
  clear_duration?: number | null
  /** Explicit clear level; null = automatic (see auto_reset_threshold). */
  reset_threshold?: number | null
  /** The automatic clear level the evaluator uses when reset_threshold is null. */
  auto_reset_threshold?: number | null
  recovery_alert?: boolean
  server_id: string | null
  target: string | null
  notify_channels?: string[]
  priority?: string | null
  escalation_policy_id?: string | null
}

/** Pre-fills the new-rule dialog, e.g. from a service or process row. */
export type HostRulePrefill = {
  metric?: string
  target?: string
  name?: string
  severity?: string
}

export type HostRuleMode = 'create' | 'edit' | 'duplicate'

/** Default clear hold the evaluator applies when a rule leaves clear_duration empty. */
export const DEFAULT_CLEAR_SECONDS = 300

export const HOST_METRICS = [
  { value: 'host_cpu_pct', label: 'CPU usage', kind: 'pct' },
  { value: 'host_memory_pct', label: 'Memory usage', kind: 'pct' },
  { value: 'host_filesystem_pct', label: 'Filesystem usage', kind: 'pct' },
  { value: 'host_disk_util_pct', label: 'Disk I/O utilization', kind: 'pct' },
  { value: 'host_service_down', label: 'Service stopped', kind: 'svc' },
  { value: 'host_process_down', label: 'Process not running', kind: 'proc' },
] as const

export type HostMetricKind = 'pct' | 'svc' | 'proc'

export const OP_SYMBOL: Record<string, string> = {
  gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠',
  '>': '>', '>=': '≥', '<': '<', '<=': '≤', '==': '=', '!=': '≠',
}

/** A rule the generic wizard must not open: its metric belongs to /host-alert-rules. */
export function isHostRule(rule: { metric?: unknown } | null | undefined): boolean {
  return String(rule?.metric ?? '').startsWith('host_')
}

export function hostMetricKind(metric: string): HostMetricKind {
  return (HOST_METRICS.find((m) => m.value === metric)?.kind as HostMetricKind | undefined) ?? 'pct'
}

export function metricLabel(metric: string): string {
  return HOST_METRICS.find((m) => m.value === metric)?.label || metric
}

export function ruleCondition(r: Pick<HostRule, 'metric' | 'operator' | 'threshold' | 'target'>): string {
  const kind = HOST_METRICS.find((x) => x.value === r.metric)?.kind
  if (kind === 'svc') return `Service ${r.target || '?'} stopped`
  if (kind === 'proc') return `Process ${r.target || '?'} not running`
  return `${metricLabel(r.metric)} ${OP_SYMBOL[r.operator] || r.operator} ${r.threshold}%`
}

export function fmtDuration(seconds: number): string {
  if (!seconds) return 'immediately'
  if (seconds < 60) return `for ${seconds}s`
  const m = Math.round(seconds / 60)
  return m >= 60 && m % 60 === 0 ? `for ${m / 60}h` : `for ${m} min`
}

/** The automatic clear level: 2 points on the healthy side of the trigger. */
export function autoReset(operator: string, threshold: number): number | null {
  if (!Number.isFinite(threshold)) return null
  const op = normalizeOperator(operator)
  if ((op === '>' || op === '>=') && threshold - 2 >= 0) return threshold - 2
  if ((op === '<' || op === '<=') && threshold + 2 <= 100) return threshold + 2
  return null
}

/** "clears below 88% for 5 min" — when an open alert resolves. */
export function ruleClearing(
  r: Pick<HostRule, 'operator' | 'threshold' | 'reset_threshold' | 'auto_reset_threshold' | 'clear_duration'>,
): string {
  const level = r.reset_threshold ?? r.auto_reset_threshold ?? autoReset(r.operator, Number(r.threshold))
  const op = normalizeOperator(r.operator)
  const below = op === '>' || op === '>='
  const hold = r.clear_duration ?? DEFAULT_CLEAR_SECONDS
  const where = level != null ? `clears ${below ? 'below' : 'above'} ${level}%` : 'clears'
  return hold ? `${where} ${fmtDuration(hold)}` : `${where} at once`
}

/* ── The form ──────────────────────────────────────────────────────────── */

export type HostRuleForm = {
  name: string
  description: string
  enabled: boolean
  metric: string
  /** gt | gte | lt | lte (the host endpoint's operator words). */
  operator: string
  threshold: string
  severity: string
  /** Breach hold in minutes. */
  minutes: string
  /** Clear hold in minutes; '' = the 300 s default. */
  clearMinutes: string
  /** Explicit clear level; '' = automatic. */
  resetLevel: string
  recovery: boolean
  target: string
  /** The server the rule is limited to when allServers is off (null = none known: the rule applies to all). */
  scopeServerId: string | null
  allServers: boolean
  channels: string[]
  /** 'auto' = derived from severity. */
  priority: string
  /** 'default' = the priority's default escalation policy. */
  policyId: string
}

const HOST_OPERATORS: Record<string, string> = { '>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte', '==': 'eq', '!=': 'neq' }
const hostOperator = (op: string | null | undefined) => HOST_OPERATORS[String(op ?? '')] ?? (op || 'gt')

const num = (v: number | null | undefined) => (v == null || !Number.isFinite(Number(v)) ? '' : String(Number(v)))
const minutesText = (seconds: number | null | undefined) => (seconds == null ? '' : String(Math.round((Number(seconds) / 60) * 100) / 100))

export function emptyHostRuleForm(serverId: string | null, prefill?: HostRulePrefill): HostRuleForm {
  return {
    name: prefill?.name || '',
    description: '',
    enabled: true,
    metric: prefill?.metric || 'host_cpu_pct',
    operator: 'gt',
    threshold: '90',
    severity: prefill?.severity || 'warning',
    minutes: '5',
    clearMinutes: '',
    resetLevel: '',
    recovery: true,
    target: prefill?.target || '',
    scopeServerId: serverId,
    allServers: !serverId,
    channels: [],
    priority: 'auto',
    policyId: 'default',
  }
}

/** The form for an existing rule (edit), or for a copy of it (duplicate: "(copy)", created disabled). */
export function hostRuleFormFromRule(rule: HostRule, mode: HostRuleMode, contextServerId: string | null = null): HostRuleForm {
  const kind = hostMetricKind(rule.metric)
  const priority = String(rule.priority ?? '').toUpperCase()
  return {
    name: mode === 'duplicate' ? `${rule.name} (copy)` : rule.name || '',
    description: rule.description || '',
    enabled: mode === 'duplicate' ? false : rule.enabled !== false,
    metric: rule.metric,
    operator: hostOperator(rule.operator),
    threshold: kind === 'pct' ? num(rule.threshold) : '0',
    severity: rule.severity || 'warning',
    minutes: minutesText(rule.min_duration ?? 0) || '0',
    clearMinutes: minutesText(rule.clear_duration),
    resetLevel: num(rule.reset_threshold),
    recovery: rule.recovery_alert === true,
    target: rule.target || '',
    scopeServerId: rule.server_id ?? contextServerId,
    allServers: !rule.server_id,
    channels: Array.isArray(rule.notify_channels) ? rule.notify_channels.map(String) : [],
    priority: ['P1', 'P2', 'P3', 'P4'].includes(priority) ? priority : 'auto',
    policyId: rule.escalation_policy_id || 'default',
  }
}

/** Client-side checks mirroring the server (422 paths match the request body). */
export function validateHostRuleForm(f: HostRuleForm): FieldIssue[] {
  const out: FieldIssue[] = []
  const kind = hostMetricKind(f.metric)
  if (kind === 'svc' || kind === 'proc') {
    if (!f.target.trim()) out.push({ path: 'target', msg: kind === 'svc' ? 'Service name is required' : 'Process name is required' })
    return out
  }
  const thr = conditionDomainError(f.metric, f.operator, f.threshold)
  if (thr) out.push({ path: 'threshold', msg: thr })
  const minutes = Number(f.minutes || 0)
  if (!Number.isFinite(minutes) || minutes < 0 || minutes * 60 > 86400) out.push({ path: 'min_duration', msg: 'Hold time is 0 to 1440 minutes' })
  if (f.clearMinutes.trim() !== '') {
    const c = Number(f.clearMinutes)
    if (!Number.isFinite(c) || c < 0 || c * 60 > 86400) out.push({ path: 'clear_duration', msg: 'Clear after is 0 to 1440 minutes' })
  }
  if (f.resetLevel.trim() !== '') {
    const r = Number(f.resetLevel)
    const domain = resetDomainError(f.metric, f.resetLevel)
    const t = Number(f.threshold)
    const op = normalizeOperator(f.operator)
    if (domain) out.push({ path: 'reset_threshold', msg: domain })
    else if (Number.isFinite(t) && (((op === '>' || op === '>=') && r > t) || ((op === '<' || op === '<=') && r < t))) {
      out.push({ path: 'reset_threshold', msg: `The clear level must be on the healthy side of the trigger (${op.startsWith('>') ? 'at or below' : 'at or above'} ${t}%)` })
    }
  }
  return out
}

function commonFields(f: HostRuleForm, mode: HostRuleMode) {
  const kind = hostMetricKind(f.metric)
  const pct = kind === 'pct'
  // A service/process rule has no hold control in the editor: a new one starts at 0, while an edited (or copied)
  // rule keeps the hold it was loaded with (the form carries it) instead of being reset to 0.
  const holdSeconds = pct || mode !== 'create' ? Math.round(Number(f.minutes || 0) * 60) : 0
  return {
    name: f.name.trim() || `${metricLabel(f.metric)} alert`,
    description: f.description.trim() || null,
    enabled: f.enabled,
    metric: f.metric,
    operator: f.operator,
    threshold: pct ? Number(f.threshold) : 0,
    severity: f.severity,
    min_duration: holdSeconds,
    // Empty = the evaluator defaults: clear after 5 minutes, 2 points on the healthy side.
    clear_duration: pct && f.clearMinutes.trim() !== '' ? Math.round(Number(f.clearMinutes) * 60) : null,
    reset_threshold: pct && f.resetLevel.trim() !== '' ? Number(f.resetLevel) : null,
    recovery_alert: f.recovery,
    target: kind === 'svc' || kind === 'proc' ? f.target.trim() : null,
    notify_channels: f.channels,
    priority: f.priority === 'auto' ? null : f.priority,
    escalation_policy_id: f.policyId === 'default' ? null : f.policyId,
  }
}

/** Fields PUT /host-alert-rules/{id} accepts (HostRuleUpdate); the scope is not one of them. */
export const HOST_RULE_UPDATE_FIELDS = [
  'name', 'description', 'enabled', 'metric', 'operator', 'threshold', 'severity', 'min_duration', 'clear_duration',
  'reset_threshold', 'recovery_alert', 'cooldown', 'notify_channels', 'target', 'priority', 'escalation_policy_id',
] as const

/** The request a save sends: POST for create/duplicate, PUT /host-alert-rules/{id} with update fields for edit. */
export function hostRuleRequest(mode: HostRuleMode, f: HostRuleForm, ruleId?: string | null): { method: 'post' | 'put'; url: string; body: Record<string, unknown> } {
  const body: Record<string, unknown> = commonFields(f, mode)
  if (mode === 'edit') {
    if (!ruleId) throw new Error('No rule to update')
    for (const k of Object.keys(body)) if (!(HOST_RULE_UPDATE_FIELDS as readonly string[]).includes(k)) delete body[k]
    return { method: 'put', url: `/host-alert-rules/${ruleId}`, body }
  }
  body.server_id = f.allServers || !f.scopeServerId ? null : f.scopeServerId
  return { method: 'post', url: '/host-alert-rules', body }
}
