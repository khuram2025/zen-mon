/**
 * Alert rule checks shared by the rule wizard and the server-rule dialog (work/RULES-COVERAGE-PLAN.md "Validation").
 *
 *  - Value domains mirror the server (network_conditions.domain_problem): a condition that can never be true, or is always true,
 *    is refused before saving with the same kind of message the server returns as a 422.
 *  - Ping status rules: the status transition decides the ping_status condition (down -> == 0, up/degraded -> == 1).
 *  - Name/description vs condition: non-blocking warnings (keyword families, direction words, "N%" vs threshold).
 *  - Field paths from a 422 map to the wizard step that owns the field.
 *
 * Pure (no imports), so scripts/test-alert-rule-editing.mjs runs it on its own.
 */

export type FieldIssue = { path: string; msg: string }
export type RuleWarning = { field: string; message: string }

/* ── Operators ─────────────────────────────────────────────────────────── */

const OP_ALIASES: Record<string, string> = { gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=' }

/** '>' for 'gt', '==' for 'eq' …; symbols pass through. */
export function normalizeOperator(op: string | null | undefined): string {
  const o = String(op ?? '').trim()
  return OP_ALIASES[o.toLowerCase()] ?? o
}

function compare(v: number, op: string, t: number): boolean {
  switch (normalizeOperator(op)) {
    case '>': return v > t
    case '>=': return v >= t
    case '<': return v < t
    case '<=': return v <= t
    case '==': return v === t
    case '!=': return v !== t
    default: return true
  }
}

/* ── Value domains ─────────────────────────────────────────────────────── */

type Domain =
  | { kind: 'discrete'; values: number[]; text: string }
  | { kind: 'range'; min: number; max: number | null; text: string }

const PERCENT_METRICS = [
  'cpu', 'memory', 'packet_loss', 'if_util_pct', 'udt_port_capacity_pct', 'netpath_loss',
  'host_cpu_pct', 'host_memory_pct', 'host_filesystem_pct', 'host_disk_util_pct',
]
const RATE_METRICS = ['apm_error_rate', 'apm_apdex', 'apm_rum_error_session_rate', 'apm_rum_resource_failure_rate']
const NON_NEGATIVE_METRICS = [
  'rtt', 'jitter', 'if_in_bps', 'if_out_bps', 'if_errors', 'if_discards', 'session_count',
  'apm_latency_p50', 'apm_latency_p95', 'apm_latency_p99', 'apm_throughput',
  'apm_rum_lcp_p75', 'apm_rum_inp_p75', 'apm_rum_cls_p75', 'apm_rum_new_error_groups',
  'apm_slo_burn', 'netpath_rtt', 'netpath_hop_count',
]

const DOMAINS: Record<string, Domain> = {
  ping_status: { kind: 'discrete', values: [0, 1], text: 'ping_status is 0 (down) or 1 (up)' },
  uptime_reset: { kind: 'discrete', values: [0, 1], text: 'uptime_reset is 1 after a reboot, otherwise 0' },
  if_oper_status: { kind: 'discrete', values: [1, 2, 3, 4, 5, 6, 7], text: 'if_oper_status is an IF-MIB code from 1 (up) to 7 (lowerLayerDown); 2 is down' },
  ...Object.fromEntries(PERCENT_METRICS.map((m) => [m, { kind: 'range', min: 0, max: 100, text: `${m} is a percentage from 0 to 100` } as Domain])),
  ...Object.fromEntries(RATE_METRICS.map((m) => [m, { kind: 'range', min: 0, max: 1, text: `${m} is a fraction from 0 to 1` } as Domain])),
  ...Object.fromEntries(NON_NEGATIVE_METRICS.map((m) => [m, { kind: 'range', min: 0, max: null, text: `${m} is never negative` } as Domain])),
}

/** Metrics whose operator and threshold the evaluator ignores (status events, "on/off" toggles). */
const IGNORED_THRESHOLD = /^(service_status|host_service_down|host_process_down|trap|syslog|udt_(new|rogue|watch)_endpoint|udt_endpoint_moved|netpath_path_change|netpath_unreachable|apm_synthetic_down|apm_anomaly)$/

export function metricDomain(metric: string): Domain | null {
  return DOMAINS[metric] ?? null
}

function fmtNum(n: number): string {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(6)))
}

/**
 * Why a condition is refused, or null: the threshold is not a finite number, or the comparison can never be true /
 * is always true for the values the metric takes. Messages read like the server's:
 * "ping_status is 0 (down) or 1 (up); '> 5' can never be true".
 */
export function conditionDomainError(metric: string, operator: string, threshold: unknown): string | null {
  if (IGNORED_THRESHOLD.test(metric)) return null
  const t = typeof threshold === 'number' ? threshold : threshold === '' || threshold == null ? NaN : Number(threshold)
  if (!Number.isFinite(t)) return 'Enter a number for the value'
  const op = normalizeOperator(operator)
  if (!['>', '>=', '<', '<=', '==', '!='].includes(op)) return `'${operator}' is not a comparison`
  const d = metricDomain(metric)
  if (!d) return null
  const shown = `'${op} ${fmtNum(t)}'`
  if (d.kind === 'discrete') {
    const hits = d.values.filter((v) => compare(v, op, t)).length
    if (hits === 0) return `${d.text}; ${shown} can never be true`
    if (hits === d.values.length) return `${d.text}; ${shown} is always true`
    return null
  }
  const { min, max } = d
  const never =
    (op === '>' && max != null && t >= max) || (op === '>=' && max != null && t > max)
    || (op === '<' && t <= min) || (op === '<=' && t < min)
    || (op === '==' && (t < min || (max != null && t > max)))
  if (never) return `${d.text}; ${shown} can never be true`
  const always =
    (op === '>' && t < min) || (op === '>=' && t <= min)
    || (op === '<' && max != null && t > max) || (op === '<=' && max != null && t >= max)
    || (op === '!=' && (t < min || (max != null && t > max)))
  if (always) return `${d.text}; ${shown} is always true`
  return null
}

/** A reset level outside the metric's range (e.g. a CPU clear level of 120%). */
export function resetDomainError(metric: string, reset: unknown): string | null {
  if (reset == null || reset === '') return null
  const r = Number(reset)
  if (!Number.isFinite(r)) return 'Enter a number for the reset value'
  const d = metricDomain(metric)
  if (d?.kind !== 'range') return null
  if (r < d.min || (d.max != null && r > d.max)) return `${d.text}; a reset level of ${fmtNum(r)} can never be reached`
  return null
}

/* ── Ping status rules ─────────────────────────────────────────────────── */

/** The ping_status condition a status transition implies; null for "any change" (left as written). */
export function pingConditionFor(trigger: string): { operator: string; threshold: number } | null {
  if (trigger === 'down') return { operator: '==', threshold: 0 }
  if (trigger === 'up' || trigger === 'degraded') return { operator: '==', threshold: 1 }
  return null
}

/** The conditions with every ping_status row matching the transition. */
export function syncPingConditions<C extends { metric: string; operator: string; threshold: number }>(conditions: C[], trigger: string): C[] {
  const want = pingConditionFor(trigger)
  if (!want) return conditions
  return conditions.map((c) => (c.metric === 'ping_status' ? { ...c, operator: want.operator, threshold: want.threshold } : c))
}

/**
 * The POST /alert-rules body that duplicates a stored rule (created disabled, "(copy)" name): seeded gt/eq
 * operators as symbols, and the ping_status condition matching the rule's trigger_on (rows the old wizard saved
 * with trigger "up" and ping_status == 0 are copied as == 1; the server normalises the same way).
 */
export function duplicateRulePayload(rule: Record<string, any>): Record<string, any> {
  const { id, created_at, updated_at, created_by, ...rest } = rule
  let conditions = Array.isArray(rest.conditions)
    ? rest.conditions.map((c: any) => (c && typeof c === 'object' ? { ...c, operator: normalizeOperator(c.operator) } : c))
    : rest.conditions
  let operator = rest.operator != null ? normalizeOperator(rest.operator) : rest.operator
  let threshold = rest.threshold
  const trigger = String(rest.trigger_on ?? 'any')
  if (Array.isArray(conditions) && conditions.length && conditions.every((c: any) => c && typeof c === 'object')) {
    conditions = syncPingConditions(conditions, trigger)
    if (conditions[0].metric === rest.metric || rest.metric == null) {
      operator = conditions[0].operator
      threshold = conditions[0].threshold
    }
  } else if (rest.metric === 'ping_status') {
    const [flat] = syncPingConditions([{ metric: 'ping_status', operator: operator ?? '==', threshold: Number(threshold) }], trigger)
    operator = flat.operator
    threshold = flat.threshold
  }
  return { ...rest, operator, threshold, conditions, name: `${rule.name} (copy)`, enabled: false }
}

/* ── Name / description vs condition ───────────────────────────────────── */

type Family = 'cpu' | 'memory' | 'disk' | 'latency' | 'loss' | 'temperature' | 'interface' | 'reboot'

const FAMILY_WORDS: Array<[Family, RegExp, string]> = [
  ['cpu', /\b(cpu|processor)\b/i, 'CPU'],
  ['memory', /\b(memory|mem|ram)\b/i, 'memory'],
  ['disk', /\b(disk|disks|filesystem|file system|volume|storage|partition)\b/i, 'disk'],
  ['latency', /\b(latency|rtt|response time|round trip|jitter)\b/i, 'latency'],
  ['loss', /\b(loss|packet loss)\b/i, 'packet loss'],
  ['temperature', /\b(temp|temperature|overheat\w*)\b/i, 'temperature'],
  ['interface', /\b(interface|interfaces|port|ports|link|uplink|bandwidth)\b/i, 'interface'],
  ['reboot', /\b(reboot\w*|restart\w*|uptime)\b/i, 'reboot'],
]

function metricFamilies(metric: string): Family[] {
  if (metric === 'cpu' || metric === 'host_cpu_pct') return ['cpu']
  if (metric === 'memory' || metric === 'host_memory_pct') return ['memory']
  if (metric === 'host_filesystem_pct' || metric === 'host_disk_util_pct') return ['disk']
  if (metric === 'rtt' || metric === 'jitter' || metric === 'netpath_rtt' || metric.startsWith('apm_latency_') || metric.startsWith('apm_rum_')) return ['latency']
  if (metric === 'packet_loss' || metric === 'netpath_loss') return ['loss']
  if (metric === 'temperature') return ['temperature']
  if (metric.startsWith('if_') || metric === 'udt_port_capacity_pct') return ['interface']
  if (metric === 'uptime_reset') return ['reboot']
  return []
}

const METRIC_NOUN: Record<string, string> = {
  ping_status: 'ping status', rtt: 'RTT', packet_loss: 'packet loss', jitter: 'jitter', cpu: 'device CPU', memory: 'device memory',
  temperature: 'temperature', uptime_reset: 'reboots', if_oper_status: 'interface status', if_util_pct: 'interface utilization',
  if_in_bps: 'inbound traffic', if_out_bps: 'outbound traffic', if_errors: 'interface errors', if_discards: 'interface discards',
  session_count: 'session count', host_cpu_pct: 'CPU usage', host_memory_pct: 'memory usage', host_filesystem_pct: 'filesystem usage',
  host_disk_util_pct: 'disk I/O utilization', host_service_down: 'a stopped service', host_process_down: 'a missing process', service_status: 'service status',
}

const PERCENT = new Set(PERCENT_METRICS)
const HIGH_WORDS = /\b(high|higher|above|exceeds?|exceeded|exceeding|over|greater|spikes?|spiking|maxed|saturated|full)\b/i
const LOW_WORDS = /\b(low|lower|below|under|less|drops?|dropped)\b/i
const UP_WORDS = /\b(comes up|came up|is up|back up|recover(s|ed|y)?|online|restored)\b/i
const DOWN_WORDS = /\b(down|offline|unreachable|unavailable|outage|fails?|failed|failure|dead|stopped)\b/i

export type RuleForWarnings = {
  name?: string | null
  description?: string | null
  conditions: Array<{ metric: string; operator: string; threshold: number | null }>
  /** ping_status / service status transition. */
  trigger_on?: string | null
}

/**
 * Non-blocking warnings when the name or description describes something the condition does not check
 * (mirrors the server's preview-warnings heuristic; the server's own warnings are shown as well).
 */
export function ruleNameWarnings(rule: RuleForWarnings): RuleWarning[] {
  const out: RuleWarning[] = []
  const conds = rule.conditions.filter((c) => c && c.metric)
  if (!conds.length) return out
  for (const field of ['name', 'description'] as const) {
    const text = String(rule[field] ?? '').trim()
    if (!text) continue
    const label = field === 'name' ? 'The name' : 'The description'
    const what = conds.map((c) => METRIC_NOUN[c.metric] ?? c.metric).filter((v, i, a) => a.indexOf(v) === i).join(' and ')

    // Keyword families: the text names metric families, and the rule checks none of them.
    const mentioned = FAMILY_WORDS.filter(([, re]) => re.test(text))
    const ruleFamilies = new Set(conds.flatMap((c) => metricFamilies(c.metric)))
    const statusOnly = conds.every((c) => ['ping_status', 'service_status', 'host_service_down', 'host_process_down'].includes(c.metric))
    // A service or process name often contains such words ("Port 443 listener", "Disk cleanup service").
    const serviceRule = conds.every((c) => ['service_status', 'host_service_down', 'host_process_down'].includes(c.metric))
    if (!serviceRule && mentioned.length && !mentioned.some(([f]) => ruleFamilies.has(f))) {
      // "Interface down" on an if_oper_status rule shares the interface family; a ping rule named after an
      // interface or a port is a mismatch.
      out.push({ field, message: `${label} mentions ${mentioned.map(([, , l]) => l).join(' and ')}, but the condition checks ${what}.` })
      continue
    }

    // Status direction: up vs down transitions.
    const trigger = rule.trigger_on ?? 'any'
    const statusRule = conds.some((c) => c.metric === 'ping_status' || c.metric === 'service_status')
    if (statusRule && trigger !== 'any') {
      if (UP_WORDS.test(text) && (trigger === 'down' || trigger === 'degraded')) {
        out.push({ field, message: `${label} says the device recovers, but the rule triggers when it goes ${trigger}.` })
        continue
      }
      if (DOWN_WORDS.test(text) && !UP_WORDS.test(text) && trigger === 'up') {
        out.push({ field, message: `${label} says down, but the rule triggers when it comes up.` })
        continue
      }
    }
    if (statusOnly) continue

    // Threshold direction: "high" with a "<" comparison and "low" with ">".
    const primary = conds.find((c) => !['ping_status', 'if_oper_status', 'uptime_reset'].includes(c.metric))
    if (primary) {
      const op = normalizeOperator(primary.operator)
      const fam = metricFamilies(primary.metric)[0]
      if (HIGH_WORDS.test(text) && (op === '<' || op === '<=')) {
        out.push({ field, message: `${label} says high, but the condition fires when ${METRIC_NOUN[primary.metric] ?? primary.metric} is below ${primary.threshold}.` })
        continue
      }
      // "Low disk space" / "low memory" mean high usage; only other metrics are checked for "low".
      if (LOW_WORDS.test(text) && (op === '>' || op === '>=') && fam !== 'disk' && fam !== 'memory' && !/\b(free|space|available|headroom)\b/i.test(text)) {
        out.push({ field, message: `${label} says low, but the condition fires when ${METRIC_NOUN[primary.metric] ?? primary.metric} is above ${primary.threshold}.` })
        continue
      }
    }

    // "N%" in the text vs a percent threshold.
    const pcts = Array.from(text.matchAll(/(\d+(?:\.\d+)?)\s*%/g)).map((m) => Number(m[1]))
    const pctConds = conds.filter((c) => PERCENT.has(c.metric) && c.threshold != null && Number.isFinite(Number(c.threshold)))
    if (pcts.length === 1 && pctConds.length && !pctConds.some((c) => Number(c.threshold) === pcts[0])) {
      out.push({ field, message: `${label} says ${fmtNum(pcts[0])}%, but the threshold is ${pctConds.map((c) => `${fmtNum(Number(c.threshold))}%`).join(', ')}.` })
    }
  }
  return out
}

/** Server warnings ({field, message} or plain strings) from a save or preview response. */
export function warningsFromApi(raw: unknown): RuleWarning[] {
  const list = raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as any).warnings : raw
  if (!Array.isArray(list)) return []
  return list.map((w: any) => (typeof w === 'string' ? { field: '', message: w } : w && typeof w === 'object' ? { field: String(w.field ?? w.loc?.[w.loc.length - 1] ?? ''), message: String(w.message ?? w.msg ?? '') } : null))
    .filter((w): w is RuleWarning => !!w && !!w.message)
}

/** Local and server warnings, each message once. */
export function mergeWarnings(...lists: RuleWarning[][]): RuleWarning[] {
  const seen = new Set<string>()
  const out: RuleWarning[] = []
  for (const w of lists.flat()) {
    const k = w.message.trim().toLowerCase()
    if (seen.has(k)) continue
    seen.add(k)
    out.push(w)
  }
  return out
}

/* ── Field path -> wizard step ─────────────────────────────────────────── */

export type WizardStepId = 'properties' | 'trigger' | 'reset' | 'scope' | 'actions' | 'escalation' | 'reset_actions' | 'schedule' | 'review'

/** The wizard step that owns a field path like `conditions[0].operator` (review when unknown). */
export function wizardStepForPath(path: string, source?: string): WizardStepId {
  const head = String(path || '').replace(/^body\.?/, '').split(/[.[]/)[0]
  if (['name', 'description', 'severity', 'priority', 'enabled'].includes(head)) return 'properties'
  if (head === 'target') return source === 'apm' ? 'scope' : 'trigger'
  if (['metric', 'operator', 'threshold', 'conditions', 'condition_logic', 'trigger_on', 'min_duration', 'duration', 'clear_duration',
    'trap_oid', 'flow_params', 'notify_while_flapping'].includes(head)) return 'trigger'
  if (head === 'recovery_alert') return 'reset'
  if (['device_id', 'group_id', 'device_type', 'location', 'scope_tag', 'service_check_id', 'service_check_group_id', 'flow_scope', 'server_id'].includes(head)) return 'scope'
  if (['notify_channels', 'cooldown', 'max_repeat', 'email_subject', 'email_body', 'sms_template'].includes(head)) return 'actions'
  if (['escalation_levels', 'escalation_policy_id'].includes(head)) return 'escalation'
  if (['recovery_email_subject', 'recovery_email_body', 'recovery_sms_template'].includes(head)) return 'reset_actions'
  if (head.startsWith('schedule_')) return 'schedule'
  return 'review'
}

/** "conditions[0].threshold" -> "Condition 1 value", "threshold" -> "Value"; unknown paths as written. */
export function fieldLabel(path: string): string {
  const p = String(path || '')
  if (!p) return ''
  const m = /^conditions\[(\d+)\]\.?(\w+)?/.exec(p)
  const leaf: Record<string, string> = { metric: 'field', operator: 'operator', threshold: 'value', reset_threshold: 'reset value' }
  if (m) return `Condition ${Number(m[1]) + 1}${m[2] ? ` ${leaf[m[2]] ?? m[2]}` : ''}`
  const flat: Record<string, string> = {
    name: 'Name', metric: 'Metric', operator: 'Operator', threshold: 'Value', target: 'Target', reset_threshold: 'Clear level',
    clear_duration: 'Clear after', min_duration: 'Hold time', trigger_on: 'Status transition', escalation_policy_id: 'Escalation policy',
    escalation_levels: 'Escalation tiers', notify_channels: 'Channels', priority: 'Priority', severity: 'Severity',
  }
  const head = p.split(/[.[]/)[0]
  return flat[p] ?? (flat[head] ? `${flat[head]}${p.slice(head.length)}` : p)
}
