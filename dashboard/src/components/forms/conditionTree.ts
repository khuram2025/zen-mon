/**
 * Combined (multi-condition) rules in the editor: the conditions each subject can combine, conditions on a linked
 * asset, per-condition holds, the read-back sentence, save checks and the stored tree.
 * Mirrors server/app/services/rule_conditions.py (docs/multi-condition-rules.md).
 */
import { OP_SYMBOL, SERVER_METRICS, serverMetric, type ServerMetric } from './serverMetrics'

export type TreeSubject = 'server' | 'device' | 'service_check'
/** pct: a reading against a limit; state: true while bad; svc/proc: a named service/process; auto: automatic services. */
export type CondKind = 'pct' | 'state' | 'svc' | 'proc' | 'auto'
export type CatalogMetric = {
  value: string
  label: string
  kind: CondKind
  unit?: string
  operators?: readonly string[]
  defaultThreshold?: number
  min?: number
  max?: number
  hint?: string
}

/** One condition. `on` reads a linked asset; `target` names an instance (or services to leave out); `for_s` is its own hold. */
export type TreeCondition = {
  id?: string
  on?: string
  metric: string
  operator: string
  threshold: number
  reset_threshold?: number | null
  target?: string
  for_s?: number | null
}
export type TreeGroup = { logic: 'AND' | 'OR'; items: TreeCondition[] }
export type ConditionTree = { logic: 'AND' | 'OR'; items: Array<TreeCondition | TreeGroup> }

/** At most this many conditions in one rule (rule_conditions.MAX_CONDITIONS). */
export const MAX_TREE_CONDITIONS = 10
/** The longest per-condition hold (rule_conditions.MAX_FOR_S). */
export const MAX_FOR_S = 86400

/** What a combined server rule can watch: levels and states. Restart, shutdown and agent offline stand alone. */
export const COMBINABLE_SERVER_METRICS: ReadonlyArray<ServerMetric> =
  SERVER_METRICS.filter((m) => m.kind === 'pct' || m.kind === 'svc' || m.kind === 'proc' || m.kind === 'auto')

export const DEVICE_CONDITIONS: ReadonlyArray<CatalogMetric> = [
  { value: 'device_down', label: 'Device is down', kind: 'state' },
  { value: 'device_degraded', label: 'Device is degraded', kind: 'state' },
  { value: 'rtt', label: 'Latency (ms)', kind: 'pct', unit: ' ms', defaultThreshold: 200, min: 0 },
  { value: 'cpu', label: 'CPU usage (%)', kind: 'pct', unit: '%', defaultThreshold: 85, min: 0, max: 100 },
  { value: 'memory', label: 'Memory usage (%)', kind: 'pct', unit: '%', defaultThreshold: 85, min: 0, max: 100 },
  { value: 'snmp_unreachable', label: 'SNMP unanswered (minutes)', kind: 'pct', unit: ' min', operators: ['>', '>='], defaultThreshold: 15, min: 0,
    hint: 'Minutes SNMP polls have gone unanswered while the device answers ping.' },
]

export const CHECK_CONDITIONS: ReadonlyArray<CatalogMetric> = [
  { value: 'service_down', label: 'Check is down', kind: 'state' },
  { value: 'service_degraded', label: 'Check is degraded', kind: 'state' },
  { value: 'service_response_ms', label: 'Response time (ms)', kind: 'pct', unit: ' ms', defaultThreshold: 2000, min: 0 },
  { value: 'service_tls_days', label: 'Certificate days left', kind: 'pct', unit: ' days', operators: ['<', '<='], defaultThreshold: 14, min: 0 },
]

const SERVER_CONDITIONS: ReadonlyArray<CatalogMetric> = COMBINABLE_SERVER_METRICS.map((m) => ({ ...m, kind: m.kind as CondKind }))

const CATALOGS: Record<TreeSubject, ReadonlyArray<CatalogMetric>> = {
  server: SERVER_CONDITIONS,
  device: DEVICE_CONDITIONS,
  service_check: CHECK_CONDITIONS,
}

/** What a condition may read besides the rule's subject (rule_conditions.LINKS). */
export const TREE_LINKS: Record<TreeSubject, ReadonlyArray<{ on: string; label: string; subject: TreeSubject }>> = {
  server: [{ on: 'device', label: 'Its device', subject: 'device' }],
  device: [{ on: 'server', label: 'Its server', subject: 'server' }],
  service_check: [
    { on: 'device', label: 'Its device', subject: 'device' },
    { on: 'server', label: 'Its server', subject: 'server' },
  ],
}
export const SELF_LABEL: Record<TreeSubject, string> = { server: 'This server', device: 'This device', service_check: 'This check' }
const LINK_WORDS: Record<string, string> = { device: 'Linked device', server: 'Linked server' }

/** Metrics with instances, and the noun of one. */
export const TREE_INSTANCE: Record<string, string> = {
  host_filesystem_pct: 'filesystem (mount)',
  host_fs_days_left: 'filesystem (mount)',
  host_disk_latency_ms: 'disk',
  host_nic_errors: 'network interface',
}

/** The subject a condition reads: the rule's own, or the linked one. */
export function readSubject(subject: TreeSubject, on?: string | null): TreeSubject | null {
  if (!on || on === 'self') return subject
  return TREE_LINKS[subject].find((l) => l.on === on)?.subject ?? null
}

export function conditionsFor(subject: TreeSubject, on?: string | null): ReadonlyArray<CatalogMetric> {
  const s = readSubject(subject, on)
  return s ? CATALOGS[s] : []
}

export function catalogMetric(subject: TreeSubject, c: Pick<TreeCondition, 'metric' | 'on'>): CatalogMetric | undefined {
  return conditionsFor(subject, c.on).find((m) => m.value === c.metric)
}

export function isTreeGroup(item: TreeCondition | TreeGroup): item is TreeGroup {
  return !!item && Array.isArray((item as TreeGroup).items)
}

export function treeConditions(tree: ConditionTree | null | undefined): TreeCondition[] {
  const out: TreeCondition[] = []
  for (const item of tree?.items || []) {
    if (isTreeGroup(item)) out.push(...item.items)
    else out.push(item)
  }
  return out
}

/** A new condition on a metric of the subject (or a linked asset), with that metric's starting limit. */
export function newCondition(subject: TreeSubject, metric?: string, on?: string): TreeCondition {
  const list = conditionsFor(subject, on)
  const spec = list.find((m) => m.value === metric) || list.find((m) => m.kind === 'pct') || list[0]
  const base: TreeCondition = spec.kind === 'pct'
    ? { metric: spec.value, operator: spec.operators?.[0] || '>', threshold: spec.defaultThreshold ?? 90, reset_threshold: null, target: '' }
    : { metric: spec.value, operator: '>', threshold: 0, target: '' }
  return on && on !== 'self' ? { ...base, on } : base
}

function holdWords(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

function conditionWords(subject: TreeSubject, c: TreeCondition): string {
  const spec = catalogMetric(subject, c)
  const kind = spec?.kind ?? 'pct'
  const target = String(c.target || '').trim()
  const link = c.on && c.on !== 'self' ? LINK_WORDS[c.on] || c.on : ''
  const hold = Number(c.for_s) > 0 ? ` for ${holdWords(Number(c.for_s))}` : ''
  let words: string
  if (kind === 'svc') words = `Service ${target || '?'} stopped`
  else if (kind === 'proc') words = `Process ${target || '?'} not running`
  else if (kind === 'auto') words = target ? `An automatic service stopped (except ${target})` : 'An automatic service stopped'
  else if (kind === 'state') {
    // "Device is down" -> "Linked device is down".
    words = link ? `${link} ${(spec?.label || c.metric).replace(/^\S+\s+/, '')}` : spec?.label || c.metric
    return words + hold
  } else {
    const label = spec ? spec.label.replace(/ \([^)]*\)$/, '') : c.metric
    const op = OP_SYMBOL[String(c.operator || '')] || c.operator || '>'
    words = `${label}${target && TREE_INSTANCE[c.metric] ? ` ${target}` : ''} ${op} ${c.threshold ?? 0}${spec?.unit ?? ''}`
  }
  return (link ? `${link}: ${words}` : words) + hold
}

/** "(CPU usage > 90% and Memory usage > 90%) or Service nginx stopped" for the list and the Review step. */
export function treeLabel(subject: TreeSubject, tree: ConditionTree): string {
  const join = (logic: string) => (logic === 'OR' ? ' or ' : ' and ')
  return (tree.items || []).map((item) => (isTreeGroup(item)
    ? `(${item.items.map((c) => conditionWords(subject, c)).join(join(item.logic))})`
    : conditionWords(subject, item))).join(join(tree.logic))
}

/** What stops a combined rule from being saved, or null (mirrors rule_conditions.validate_tree). */
export function treeProblem(subject: TreeSubject, tree: ConditionTree): string | null {
  const all = treeConditions(tree)
  if (all.length < 2) return 'A combined rule needs at least two conditions (or use one condition).'
  if (all.length > MAX_TREE_CONDITIONS) return `A rule can combine at most ${MAX_TREE_CONDITIONS} conditions.`
  for (const item of tree.items) {
    if (isTreeGroup(item) && item.items.length < 2) return 'A group holds at least two conditions.'
  }
  if (subject === 'server' && !all.some((c) => !c.on || c.on === 'self')) {
    return 'A server rule needs at least one condition on the server itself.'
  }
  for (const c of all) {
    const spec = catalogMetric(subject, c)
    if (!spec) return `${c.metric} cannot be combined here.`
    const target = String(c.target || '').trim()
    if (spec.kind === 'svc' && !target) return 'Enter the service name.'
    if (spec.kind === 'proc' && !target) return 'Enter the process name.'
    const hold = c.for_s == null || String(c.for_s) === '' ? 0 : Number(c.for_s)
    if (!Number.isFinite(hold) || hold < 0 || hold > MAX_FOR_S) return 'A condition’s hold is 0 to 86400 seconds.'
    if (spec.kind !== 'pct') continue
    const thr = Number(c.threshold)
    if (!Number.isFinite(thr)) return `${spec.label}: enter the limit.`
    if (spec.unit === '%' && !(thr >= 0 && thr <= 100)) return `${spec.label}: the limit is a percentage from 0 to 100.`
    if (spec.operators && !spec.operators.includes(OP_SYMBOL[c.operator] || c.operator)) return `${spec.label}: use ${spec.operators.join(' or ')}.`
    if ((c.metric === 'host_fs_days_left' || c.metric === 'service_tls_days') && !(thr > 0)) return `${spec.label}: the limit must be more than 0.`
    if (c.reset_threshold != null && String(c.reset_threshold) !== '' && Number.isFinite(Number(c.reset_threshold))) {
      const r = Number(c.reset_threshold)
      const high = ['>', '>=', 'gt', 'gte'].includes(c.operator)
      if ((high && r > thr) || (!high && r < thr)) return `${spec.label}: the clear level must be on the healthy side of the limit.`
    }
  }
  return null
}

/** The tree as the API stores it. */
export function conditionTreePayload(subject: TreeSubject, tree: ConditionTree): ConditionTree {
  const cond = (c: TreeCondition): TreeCondition => {
    const kind = catalogMetric(subject, c)?.kind ?? 'pct'
    const target = String(c.target || '').trim()
    const hold = Number(c.for_s) > 0 ? Math.round(Number(c.for_s)) : 0
    return {
      ...(c.id ? { id: c.id } : {}),
      ...(c.on && c.on !== 'self' ? { on: c.on } : {}),
      metric: c.metric,
      operator: kind === 'pct' ? (OP_SYMBOL[c.operator] || c.operator) : '>',
      threshold: kind === 'pct' ? Number(c.threshold) : 0,
      ...(kind === 'pct' && c.reset_threshold != null && String(c.reset_threshold) !== '' ? { reset_threshold: Number(c.reset_threshold) } : {}),
      ...(target ? { target } : {}),
      ...(hold ? { for_s: hold } : {}),
    }
  }
  return { logic: tree.logic, items: tree.items.map((i) => (isTreeGroup(i) ? { logic: i.logic, items: i.items.map(cond) } : cond(i))) }
}

/** Two starting conditions for a new combined device or service-check rule. */
export function starterTree(subject: TreeSubject): ConditionTree {
  if (subject === 'service_check') return { logic: 'AND', items: [newCondition('service_check', 'service_response_ms'), newCondition('service_check', 'host_cpu_pct', 'server')] }
  if (subject === 'device') return { logic: 'AND', items: [newCondition('device', 'cpu'), newCondition('device', 'memory')] }
  return { logic: 'AND', items: [newCondition('server', 'host_cpu_pct'), newCondition('server', 'host_memory_pct')] }
}

export { serverMetric }
