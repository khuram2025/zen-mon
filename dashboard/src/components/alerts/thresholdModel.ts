/* Thresholds per class (Device Templates phase 3, migrate-193; docs/alert-rule-overrides.md).
 *
 * Interface roles, recommended rules, per-asset overrides and effective thresholds share these words:
 * the role catalogue, a condition read out ("CPU ≥ 90 % for 5 min"), what an override changes and which
 * rules can be overridden. No React here, so scripts/test-rule-overrides-ui.mjs can pin the rules. */

export const INTERFACE_ROLES = ['uplink', 'access', 'wan', 'server', 'tunnel', 'vlan', 'loopback', 'management'] as const
export type InterfaceRole = (typeof INTERFACE_ROLES)[number]

/** server/app/services/interface_roles.ROLE_LABELS */
export const ROLE_LABEL: Record<string, string> = {
  uplink: 'Uplink', access: 'Access', wan: 'WAN', server: 'Server-facing',
  tunnel: 'Tunnel', vlan: 'VLAN', loopback: 'Loopback', management: 'Management',
}

export function roleLabel(role: string | null | undefined): string {
  return role ? ROLE_LABEL[role] || role : '—'
}

export const ROLE_FIELDS = [
  { value: 'if_type', label: 'Interface type (ifType)', hint: 'IF-MIB type numbers, comma separated, e.g. 6,117' },
  { value: 'if_name', label: 'Name (ifName)', hint: 'Regular expression, e.g. ^(Gi|Te)1/0/(49|50)$' },
  { value: 'if_alias', label: 'Description (ifAlias)', hint: 'Regular expression, e.g. (?i)uplink|core' },
  { value: 'if_descr', label: 'ifDescr', hint: 'Regular expression' },
] as const

export type RoleRule = { role: string; field: string; pattern: string }

/** Problems in a role list the server would refuse (server/app/services/interface_roles.validate), by row. */
export function roleRuleProblems(rules: RoleRule[]): string[] {
  const out: string[] = []
  rules.forEach((r, i) => {
    const n = i + 1
    const p = (r.pattern || '').trim()
    if (!INTERFACE_ROLES.includes(r.role as InterfaceRole)) out.push(`Row ${n}: choose a role`)
    if (!p) out.push(`Row ${n}: the pattern is required`)
    else if (p.length > 200) out.push(`Row ${n}: at most 200 characters`)
    else if (r.field === 'if_type') {
      if (!p.split(',').map((x) => x.trim()).filter(Boolean).every((x) => /^\d+$/.test(x))) {
        out.push(`Row ${n}: type numbers separated by commas`)
      }
    } else {
      try {
        // Python's inline (?i) flag is not JavaScript syntax; strip it for the browser check only.
        new RegExp(p.replace(/^\(\?i\)/, ''))
      } catch {
        out.push(`Row ${n}: not a valid regular expression`)
      }
    }
  })
  if (rules.length > 50) out.push('At most 50 role rules')
  return out
}

// ── Metrics and conditions in words ────────────────────────────────────────

export const INTERFACE_METRIC_KEYS = [
  'if_util_pct', 'if_in_bps', 'if_out_bps', 'if_errors', 'if_discards', 'if_oper_status',
  'if_error_rate_pct', 'if_discard_rate_pct', 'if_flaps', 'if_speed_changed',
] as const
const SCALAR_METRIC_KEYS = ['cpu', 'memory', 'temperature', 'session_count']
// host_alert_service.THRESHOLD_METRICS: history metrics and the clock offset (migrate-208 added swap, disk
// latency, NIC errors and clock offset). Snapshot metrics (days until full, automatic services) take no override.
const SERVER_THRESHOLD_METRICS = ['host_cpu_pct', 'host_memory_pct', 'host_filesystem_pct', 'host_disk_util_pct',
  'host_swap_pct', 'host_disk_latency_ms', 'host_nic_errors', 'host_clock_skew_s']

const METRIC_LABEL: Record<string, string> = {
  if_util_pct: 'Interface utilisation', if_in_bps: 'Interface inbound', if_out_bps: 'Interface outbound',
  if_errors: 'Interface errors', if_discards: 'Interface discards', if_oper_status: 'Interface status',
  if_error_rate_pct: 'Interface error rate', if_discard_rate_pct: 'Interface discard rate',
  if_flaps: 'Interface state changes', if_speed_changed: 'Interface speed change',
  cpu: 'CPU', memory: 'Memory', temperature: 'Temperature', session_count: 'Sessions', uptime_reset: 'Reboot',
  ping_status: 'Ping status', rtt: 'RTT', packet_loss: 'Packet loss', jitter: 'Jitter',
  host_cpu_pct: 'CPU', host_memory_pct: 'Memory', host_filesystem_pct: 'Filesystem usage', host_disk_util_pct: 'Disk I/O',
  host_swap_pct: 'Swap', host_disk_latency_ms: 'Disk latency', host_nic_errors: 'Network errors', host_clock_skew_s: 'Clock offset',
  host_fs_days_left: 'Filesystem full in',
}

const METRIC_UNIT: Record<string, string> = {
  if_util_pct: '%', if_error_rate_pct: '% of packets', if_discard_rate_pct: '% of packets', cpu: '%', memory: '%', packet_loss: '%', rtt: ' ms', jitter: ' ms', if_in_bps: ' bps', if_out_bps: ' bps',
  host_cpu_pct: '%', host_memory_pct: '%', host_filesystem_pct: '%', host_disk_util_pct: '%', temperature: ' °C',
  host_swap_pct: '%', host_disk_latency_ms: ' ms', host_nic_errors: ' errors/s', host_clock_skew_s: ' s', host_fs_days_left: ' days',
}

const OP_WORD: Record<string, string> = {
  '>': '>', gt: '>', '>=': '≥', gte: '≥', '<': '<', lt: '<', '<=': '≤', lte: '≤', '==': '=', eq: '=', '!=': '≠', neq: '≠',
}

export function isTemplateMetric(metric: string | null | undefined): boolean {
  return String(metric || '').startsWith('tpl_')
}

/** A readable metric name; template metrics use `names` (key → name) when given. */
export function metricWords(metric: string | null | undefined, names?: Record<string, string>): string {
  const m = String(metric || '')
  if (isTemplateMetric(m)) return names?.[m.slice(4)] || `${m.slice(4)} (template)`
  return METRIC_LABEL[m] || m
}

/** "for 5 min", "at once" for a breach hold in seconds. */
export function holdWords(seconds: number | null | undefined): string {
  const s = Number(seconds) || 0
  if (s <= 0) return 'at once'
  if (s % 3600 === 0) return `for ${s / 3600} h`
  if (s % 60 === 0) return `for ${s / 60} min`
  return `for ${s} s`
}

/** "5 min", "90 s" for a duration field. */
export function durationWords(seconds: number | null | undefined): string {
  const s = Number(seconds) || 0
  if (s % 3600 === 0 && s >= 3600) return `${s / 3600} h`
  if (s % 60 === 0 && s >= 60) return `${s / 60} min`
  return `${s} s`
}

function num(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return '?'
  const n = Number(v)
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100)
}

export type Condition = { metric: string; operator: string; threshold: number | null; reset_threshold?: number | null }

/** One condition: "CPU ≥ 90 %" ("Interface status down" for oper status rules). */
export function conditionWords(c: Condition, opts: { unit?: string | null; names?: Record<string, string> } = {}): string {
  if (c.metric === 'if_oper_status') return 'Interface down'
  if (c.metric === 'uptime_reset') return 'Device restarted'
  if (c.metric === 'if_speed_changed') return 'Interface speed changed'
  const unit = opts.unit != null ? (opts.unit === '%' ? '%' : opts.unit ? ` ${opts.unit}` : '') : METRIC_UNIT[c.metric] || ''
  const base = `${metricWords(c.metric, opts.names)} ${OP_WORD[c.operator] || c.operator} ${num(c.threshold)}${unit}`
  return c.reset_threshold != null ? `${base} (clears at ${num(c.reset_threshold)}${unit})` : base
}

/** A rule's conditions and hold: "CPU ≥ 90 % for 5 min". */
export function ruleWords(r: { conditions: Condition[]; min_duration?: number | null }, names?: Record<string, string>): string {
  const conds = (r.conditions || []).map((c) => conditionWords(c, { names })).join(' and ')
  const metrics = (r.conditions || []).map((c) => c.metric)
  // Flapping counts changes within its window (the hold, default 15 min);
  // a speed change is an event with no hold.
  if (metrics.includes('if_flaps')) return `${conds} in ${durationWords(Number(r.min_duration) || 900)}`
  if (metrics.includes('if_speed_changed')) return conds
  return `${conds} ${holdWords(r.min_duration)}`.trim()
}

// ── Recommendations ─────────────────────────────────────────────────────────

const FIELD_WORD: Record<string, string> = {
  metric: 'metric', operator: 'operator', threshold: 'threshold', reset_threshold: 'clear level',
  min_duration: 'hold', clear_duration: 'recovery hold', severity: 'severity', priority: 'priority', if_role: 'interface role',
  states: 'alerting states',
}

function valueWords(field: string, v: unknown): string {
  if (v == null || v === '') return field === 'priority' ? 'from severity' : 'none'
  // A state recommendation's bad states (codes, or their labels when the caller resolved them).
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === 'number' ? num(x) : String(x))).join(', ') : 'none'
  if (field === 'min_duration' || field === 'clear_duration') return durationWords(Number(v))
  if (field === 'if_role') return roleLabel(String(v))
  if (typeof v === 'number') return num(v)
  return String(v)
}

/** "threshold 90 → 95" for one changed recommendation field. */
export function changeWords(ch: { field: string; from: unknown; to: unknown }): string {
  return `${FIELD_WORD[ch.field] || ch.field} ${valueWords(ch.field, ch.from)} → ${valueWords(ch.field, ch.to)}`
}

// ── Overrides ───────────────────────────────────────────────────────────────

export type RuleLike = { metric?: string | null; conditions?: Condition[] | null }

function ruleMetrics(r: RuleLike): string[] {
  const conds = Array.isArray(r.conditions) && r.conditions.length ? r.conditions : [{ metric: r.metric || '' } as Condition]
  return conds.map((c) => String(c.metric || ''))
}

/**
 * Whether a rule takes overrides (alert_rules._overridable): SNMP interface/device metrics and template metrics,
 * or server threshold metrics; never status, trap, syslog, service, NetFlow or APM rules, nor a restart rule.
 */
export function ruleTakesOverrides(r: RuleLike): boolean {
  const ms = ruleMetrics(r)
  if (!ms.length || ms.every((m) => m === 'uptime_reset')) return false
  const network = new Set<string>([...INTERFACE_METRIC_KEYS, ...SCALAR_METRIC_KEYS, 'uptime_reset'])
  if (ms.every((m) => network.has(m) || isTemplateMetric(m))) return true
  return ms.every((m) => SERVER_THRESHOLD_METRICS.includes(m))
}

/** Device rule or server rule: decides whether rows name a device or a server. */
export function overrideAssetKind(r: RuleLike): 'device' | 'server' {
  return SERVER_THRESHOLD_METRICS.includes(String(r.metric || '')) ? 'server' : 'device'
}

/** Whether a rule watches instances (interfaces, filesystems, template table rows), so a pattern means something. */
export function ruleHasInstances(r: RuleLike): boolean {
  return ruleMetrics(r).some((m) => (INTERFACE_METRIC_KEYS as readonly string[]).includes(m) || isTemplateMetric(m)
    || m === 'host_filesystem_pct' || m === 'host_disk_latency_ms' || m === 'host_nic_errors' || m === 'temperature')
}

export type OverrideChanges = {
  metric?: string | null
  threshold?: number | null
  reset_threshold?: number | null
  min_duration?: number | null
  clear_duration?: number | null
  priority?: string | null
}

/** "threshold 95, hold 10 min, priority P3" */
export function overrideChangeWords(o: OverrideChanges, names?: Record<string, string>): string {
  const parts: string[] = []
  const on = o.metric ? ` (${metricWords(o.metric, names)})` : ''
  if (o.threshold != null) parts.push(`threshold ${num(o.threshold)}${on}`)
  if (o.reset_threshold != null) parts.push(`clear level ${num(o.reset_threshold)}${o.threshold == null ? on : ''}`)
  if (o.min_duration != null) parts.push(`hold ${durationWords(o.min_duration)}`)
  if (o.clear_duration != null) parts.push(`recovery hold ${durationWords(o.clear_duration)}`)
  if (o.priority) parts.push(`priority ${o.priority}`)
  return parts.join(', ') || 'no change'
}

/** The value of a datetime-local input for an ISO time (local clock), '' for none. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** ISO (UTC) for a datetime-local value, null for ''. */
export function fromLocalInput(v: string): string | null {
  if (!v) return null
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

/** Effective threshold source badge text. */
export const SOURCE_LABEL: Record<string, string> = {
  rule: 'rule', override: 'override', excluded: 'excluded', disabled: 'off',
}
