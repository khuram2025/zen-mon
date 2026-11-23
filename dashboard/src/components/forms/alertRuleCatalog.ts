/**
 * Alert rule catalogue: every rule metric the server accepts, the family it belongs to, the inputs it
 * takes and which timers its evaluator honours, the server validators mirrored for the wizard, the
 * rule list's words, and the template state (enum) helpers.
 *
 * Sources: server/app/api/v1/alert_rules.py (_validate_service_metric, _validate_appliance_conditions,
 * _validate_virtualization_metric, _validate_monitoring_gap, _validate_template_states),
 * server/app/services/network_conditions.py (interface quality, retired keys), template_states.py,
 * docs/appliance-health-alerts.md, service-metric-alerts.md, virtualization-alerts.md,
 * monitoring-gap-alerts.md, alert-notification-policy.md and alert-rule-overrides.md.
 *
 * No React and no '@/' imports, so scripts/test-alert-rule-catalog.mjs runs it on its own.
 */
import { serverMetricLabel, serverRuleCondition } from './serverAlertRule'
import { treeLabel } from './conditionTree'
import { APM_EVENT_TRIGGERS } from './apmEventTriggers'

// ── Families (the wizard's sources) ──────────────────────────────────────────

export type RuleSource =
  | 'device' | 'server' | 'service' | 'gap' | 'appliance' | 'virtualization'
  | 'udt' | 'netpath' | 'trap' | 'syslog' | 'apm' | 'netflow'

/** The wizard's source buttons and the rule list's family filter, in that order. */
export const RULE_SOURCES: ReadonlyArray<{ value: RuleSource; label: string; hint: string }> = [
  { value: 'device', label: 'Device / SNMP', hint: 'Reachability, latency, SNMP and template metrics, interfaces, F5 certificates' },
  { value: 'server', label: 'Server', hint: 'Agent CPU, memory, filesystems, services, processes, restarts' },
  { value: 'service', label: 'Service Check', hint: 'Service check status, certificate expiry, response time' },
  { value: 'gap', label: 'Monitoring gap', hint: 'SNMP polls unanswered, agent sending no metrics' },
  { value: 'virtualization', label: 'Virtualisation', hint: 'ESXi hosts, datastores, vCenter and Prism alarms' },
  { value: 'udt', label: 'Device Tracker', hint: 'New, rogue, watched and moved endpoints, switch port usage' },
  { value: 'netpath', label: 'NetPath', hint: 'Path latency, loss, hops, route changes, unreachable targets' },
  { value: 'appliance', label: 'ZenPlus appliance', hint: 'Data disk, database, backups, updates, licence, sign-ins' },
  { value: 'trap', label: 'SNMP Trap', hint: 'Traps received from devices' },
  { value: 'syslog', label: 'Syslog', hint: 'Syslog messages by severity' },
  { value: 'apm', label: 'APM & RUM', hint: 'Application latency, errors, synthetic journeys, SLOs' },
  { value: 'netflow', label: 'NetFlow', hint: 'Traffic volume, endpoints, interface utilisation, findings' },
]

export const SOURCE_LABEL: Record<RuleSource, string> = Object.fromEntries(
  RULE_SOURCES.map((s) => [s.value, s.label])) as Record<RuleSource, string>

const SERVICE_METRICS = new Set(['service_tls_days', 'service_response_ms'])
const VIRT_METRICS = new Set(['vm_host_disconnected', 'vm_datastore_pct', 'vm_alarm'])
const GAP_METRICS = new Set(['snmp_unreachable', 'agent_no_metrics'])

/** The family a stored rule belongs to (the wizard source it opens on; the rule list's filter). */
export function ruleSource(r: { metric?: string | null; service_check_id?: string | null; service_check_group_id?: string | null; subject_type?: string | null }): RuleSource {
  const m = String(r.metric || '')
  // A combined rule (migrate-212) belongs to its subject's family.
  if (m === 'combined') return r.subject_type === 'service_check' ? 'service' : r.subject_type === 'server' ? 'server' : 'device'
  if (m.startsWith('netflow_')) return 'netflow'
  if (m === 'syslog') return 'syslog'
  if (m === 'trap') return 'trap'
  if (m.startsWith('apm_')) return 'apm'
  if (m.startsWith('host_')) return 'server'
  if (m.startsWith('appliance_')) return 'appliance'
  if (VIRT_METRICS.has(m)) return 'virtualization'
  if (GAP_METRICS.has(m)) return 'gap'
  if (m.startsWith('udt_')) return 'udt'
  if (m.startsWith('netpath_')) return 'netpath'
  if (r.service_check_id || r.service_check_group_id || m === 'service_status' || SERVICE_METRICS.has(m)) return 'service'
  return 'device'
}

// ── Operators ────────────────────────────────────────────────────────────────

export const ALL_OPERATORS = ['>', '>=', '<', '<=', '==', '!='] as const
const OP_SYMBOL: Record<string, string> = { gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=' }
const OP_WORD: Record<string, string> = { '>': '>', '>=': '≥', '<': '<', '<=': '≤', '==': '=', '!=': '≠' }
const ORDER_OPS = ['>', '>=', '<', '<=']
const HIGH_OPS = ['>', '>=']

/** A stored operator as the editor's lists show it: word operators (gt) become symbols (>). */
export function normalizeOperator(op: unknown): string {
  const o = String(op ?? '').trim()
  return OP_SYMBOL[o.toLowerCase()] || o
}

function cmp(v: number, op: string, t: number): boolean {
  switch (normalizeOperator(op)) {
    case '>': return v > t
    case '>=': return v >= t
    case '<': return v < t
    case '<=': return v <= t
    case '==': return v === t
    case '!=': return v !== t
    default: return false
  }
}

// ── Metrics ─────────────────────────────────────────────────────────────────

/**
 * level: compared with a limit (optional clear level); flag: 1 while a state is bad, fixed condition;
 * event: something happened (fixed condition, no breach hold); status: device/service status transition.
 */
export type MetricKind = 'level' | 'flag' | 'event' | 'status'

export type MetricSpec = {
  key: string
  /** Picker label. */
  label: string
  /** Words in the rule list and summaries ("Data disk used"). */
  short: string
  source: RuleSource
  kind: MetricKind
  /** Appended to a number: '%', ' ms', ' days', ' min'. */
  unit?: string
  /** Operators offered for a new rule (the stored one is always shown too). */
  operators?: readonly string[]
  /** The condition a flag/event metric always stores. */
  fixed?: { operator: string; threshold: number }
  /** Operator and limit a new condition starts with. */
  defaults?: { operator: string; threshold: number }
  min?: number
  max?: number
  /** Whether a clear level (reset_threshold) may be set, and the automatic one's words. */
  clearLevel?: { auto: string } | false
  /** Only condition of its rule. */
  single?: boolean
  /** Why new rules may not use it; existing rules keep loading and stay editable. */
  retired?: string
  /** No collector at all (network_conditions.UNMAPPED_METRICS): refused even on a rule that already has it. */
  unmapped?: boolean
  /** Shown under the condition in the wizard. */
  hint?: string
  /** What the rule's target names (appliance channel/sensor rules). */
  target?: 'channel' | 'sensor'
}

const pctLevel = { unit: '%', operators: ORDER_OPS, min: 0, max: 100 } as const

export const METRICS: ReadonlyArray<MetricSpec> = [
  // Device status (alert_engine): evaluated on confirmed status changes.
  { key: 'ping_status', label: 'Ping status', short: 'Reachability', source: 'device', kind: 'status' },
  { key: 'rtt', label: 'Latency (RTT, ms)', short: 'Latency', source: 'device', kind: 'status', unit: ' ms',
    defaults: { operator: '>', threshold: 100 },
    hint: 'Fires only when the device’s confirmed status is Degraded (Settings → Monitoring limits): a move into Down carries no latency reading, so Device Down pages for outages.' },
  { key: 'packet_loss', label: 'Packet loss (%)', short: 'Packet loss', source: 'device', kind: 'status', unit: '%',
    defaults: { operator: '>', threshold: 5 },
    hint: 'Fires only when the device’s confirmed status is Degraded (Settings → Monitoring limits): a move into Down carries no loss reading, so Device Down pages for outages.' },
  { key: 'jitter', label: 'Jitter (ms)', short: 'Jitter', source: 'device', kind: 'status', unit: ' ms',
    retired: 'Device status checks do not measure jitter, so this rule never fires. Use a NetPath rule or the template metric SD-WAN Health: Jitter.' },
  // SNMP interface metrics (network_alert_service).
  { key: 'if_util_pct', label: 'Interface utilisation (%)', short: 'Interface utilisation', source: 'device', kind: 'level', ...pctLevel, defaults: { operator: '>', threshold: 80 } },
  { key: 'if_in_bps', label: 'Interface inbound (bps)', short: 'Interface inbound', source: 'device', kind: 'level', unit: ' bps' },
  { key: 'if_out_bps', label: 'Interface outbound (bps)', short: 'Interface outbound', source: 'device', kind: 'level', unit: ' bps' },
  { key: 'if_errors', label: 'Interface errors (per poll)', short: 'Interface errors', source: 'device', kind: 'level', unit: ' errors' },
  { key: 'if_discards', label: 'Interface discards (per poll)', short: 'Interface discards', source: 'device', kind: 'level', unit: ' discards' },
  { key: 'if_error_rate_pct', label: 'Interface error rate (% of packets)', short: 'Interface error rate', source: 'device', kind: 'level',
    unit: '% of packets', operators: ORDER_OPS, min: 0, max: 100, defaults: { operator: '>', threshold: 1 },
    hint: 'Errors as a share of the packets in each poll interval, the worse direction. Intervals under 1,000 packets are not judged.' },
  { key: 'if_discard_rate_pct', label: 'Interface discard rate (% of packets)', short: 'Interface discard rate', source: 'device', kind: 'level',
    unit: '% of packets', operators: ORDER_OPS, min: 0, max: 100, defaults: { operator: '>', threshold: 1 },
    hint: 'Discards as a share of the packets in each poll interval, the worse direction. Intervals under 1,000 packets are not judged.' },
  { key: 'if_flaps', label: 'Interface flapping (state changes)', short: 'Interface state changes', source: 'device', kind: 'level',
    unit: ' changes', operators: ['>', '>='], min: 0, single: true, clearLevel: false, defaults: { operator: '>=', threshold: 5 },
    hint: 'Counts changes into or out of up within the window below. Clears once a whole window passes without a change.' },
  { key: 'if_speed_changed', label: 'Interface speed changed', short: 'Interface speed change', source: 'device', kind: 'event',
    fixed: { operator: '==', threshold: 1 }, single: true,
    hint: 'Fires when the negotiated speed of an up interface changes (for example 1 Gb/s to 100 Mb/s). Speeds reported while the link is down never count.' },
  { key: 'if_oper_status', label: 'Interface down (oper status)', short: 'Interface down', source: 'device', kind: 'level' },
  // SNMP device metrics.
  { key: 'cpu', label: 'Device CPU (%)', short: 'CPU', source: 'device', kind: 'level', ...pctLevel, defaults: { operator: '>', threshold: 90 } },
  { key: 'memory', label: 'Device memory (%)', short: 'Memory', source: 'device', kind: 'level', ...pctLevel, defaults: { operator: '>', threshold: 90 } },
  { key: 'temperature', label: 'Device temperature (per sensor)', short: 'Temperature', source: 'device', kind: 'level', unit: ' °C',
    defaults: { operator: '>', threshold: 70 },
    hint: 'One incident per ENTITY-SENSOR-MIB temperature sensor (Celsius sensors).' },
  { key: 'uptime_reset', label: 'Reboot detected', short: 'Reboot', source: 'device', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'f5_cert_days', label: 'F5 certificate days left', short: 'F5 certificate expiry', source: 'device', kind: 'level', unit: ' days',
    operators: ORDER_OPS, min: -3650, max: 3650, single: true, clearLevel: { auto: '1 day on the healthy side' },
    defaults: { operator: '<', threshold: 30 },
    hint: 'Every certificate in an F5 BIG-IP’s certificate inventory, including unused ones; the inventory does not show whether a certificate serves live traffic.' },
  { key: 'session_count', label: 'Session count', short: 'Session count', source: 'device', kind: 'level',
    retired: 'Session count is not collected under this name. Use the template metric for the device’s session table (Active Sessions, Current Connections).' },
  ...(['fan_state', 'psu_state', 'vpn_tunnel_state', 'ha_state', 'bgp_neighbor_down'] as const).map((key): MetricSpec => ({
    key, label: ({ fan_state: 'Fan state', psu_state: 'Power supply state', vpn_tunnel_state: 'VPN tunnel state', ha_state: 'HA state', bgp_neighbor_down: 'BGP neighbour down' })[key],
    short: ({ fan_state: 'Fan state', psu_state: 'Power supply state', vpn_tunnel_state: 'VPN tunnel state', ha_state: 'HA state', bgp_neighbor_down: 'BGP neighbour down' })[key],
    source: 'device', kind: 'level', unmapped: true,
    retired: 'No collector writes this key: hardware and protocol state is collected per component by device templates. Use the template’s state metric (Template metric… → a State metric), or accept the template’s recommended state rule.',
  })),
  // Service checks.
  { key: 'service_status', label: 'Service status change', short: 'Service status', source: 'service', kind: 'status' },
  { key: 'service_tls_days', label: 'Certificate days left', short: 'Certificate expiry', source: 'service', kind: 'level', unit: ' days',
    operators: ORDER_OPS, min: -3650, max: 3650, single: true, clearLevel: { auto: '1 day on the healthy side' },
    defaults: { operator: '<', threshold: 30 },
    hint: 'Days until the certificate the check last saw expires, read only from a fresh run; an expired certificate reads negative.' },
  { key: 'service_response_ms', label: 'Response time (ms)', short: 'Response time', source: 'service', kind: 'level', unit: ' ms',
    operators: ORDER_OPS, min: 0, single: true, clearLevel: { auto: '10% on the healthy side' },
    defaults: { operator: '>', threshold: 1500 },
    hint: 'Average of the check’s passing runs over the hold (at least 5 minutes). Failed runs never count: status rules handle them.' },
  // Monitoring gaps.
  { key: 'snmp_unreachable', label: 'SNMP polls unanswered (minutes)', short: 'SNMP polls unanswered', source: 'gap', kind: 'level', unit: ' min',
    operators: ['>', '>='], min: 1, max: 10080, single: true, clearLevel: false, defaults: { operator: '>=', threshold: 15 },
    hint: 'Minutes the controller’s SNMP polls have gone unanswered while the device answers ping (Up or Degraded). Devices polled only by a remote sensor have no reading.' },
  { key: 'agent_no_metrics', label: 'Agent sends no metrics (minutes)', short: 'Agent sends no metrics', source: 'gap', kind: 'level', unit: ' min',
    operators: ['>', '>='], min: 1, max: 10080, single: true, clearLevel: false, defaults: { operator: '>=', threshold: 15 },
    hint: 'Minutes since the agent’s last host metric batch while it still sends heartbeats. A silent agent is the availability rules’ business.' },
  // Virtualisation.
  { key: 'vm_host_disconnected', label: 'ESXi host disconnected', short: 'Host disconnected', source: 'virtualization', kind: 'flag',
    fixed: { operator: '==', threshold: 1 },
    hint: 'vSphere only: 1 while vCenter reports a host disconnected or not responding. Hosts in maintenance mode are not evaluated.' },
  { key: 'vm_datastore_pct', label: 'Datastore used (%)', short: 'Datastore used', source: 'virtualization', kind: 'level', ...pctLevel,
    single: true, clearLevel: { auto: '2 points on the healthy side' }, defaults: { operator: '>', threshold: 85 } },
  { key: 'vm_alarm', label: 'Platform alarm (vCenter / Prism)', short: 'Platform alarm', source: 'virtualization', kind: 'flag',
    operators: ['>='], defaults: { operator: '>=', threshold: 2 }, clearLevel: false, single: true,
    hint: 'vCenter triggered alarms and Prism alerts. An alarm that leaves a current snapshot has cleared.' },
  // Device Tracker.
  { key: 'udt_new_endpoint', label: 'New endpoint', short: 'New endpoint', source: 'udt', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'udt_rogue_endpoint', label: 'Rogue endpoint', short: 'Rogue endpoint', source: 'udt', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'udt_watch_endpoint', label: 'Watched endpoint seen', short: 'Watched endpoint seen', source: 'udt', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'udt_endpoint_moved', label: 'Endpoint moved', short: 'Endpoint moved', source: 'udt', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'udt_port_capacity_pct', label: 'Switch port usage (%)', short: 'Switch port usage', source: 'udt', kind: 'level',
    unit: '%', operators: ['>', '>=', '<', '<='], min: 0, max: 100, clearLevel: false, defaults: { operator: '>=', threshold: 90 },
    hint: 'Ports in use as a share of a switch’s ports. Resolves at the next evaluation below the limit.' },
  // NetPath.
  { key: 'netpath_rtt', label: 'Path latency (ms)', short: 'Path latency', source: 'netpath', kind: 'level', unit: ' ms',
    operators: ORDER_OPS, min: 0, clearLevel: false, defaults: { operator: '>', threshold: 150 } },
  { key: 'netpath_loss', label: 'Path packet loss (%)', short: 'Path packet loss', source: 'netpath', kind: 'level', ...pctLevel,
    clearLevel: false, defaults: { operator: '>', threshold: 5 } },
  { key: 'netpath_hop_count', label: 'Hop count', short: 'Path hop count', source: 'netpath', kind: 'level', unit: ' hops',
    operators: ORDER_OPS, min: 0, clearLevel: false, defaults: { operator: '>', threshold: 20 } },
  { key: 'netpath_path_change', label: 'Route changed', short: 'Route changed', source: 'netpath', kind: 'event', fixed: { operator: '==', threshold: 1 } },
  { key: 'netpath_unreachable', label: 'Target unreachable', short: 'Target unreachable', source: 'netpath', kind: 'flag', fixed: { operator: '==', threshold: 1 },
    hint: 'Raised when the probe can no longer reach its target; resolved when it reaches it again.' },
  // ZenPlus appliance.
  { key: 'appliance_disk_pct', label: 'Data disk used (%)', short: 'Data disk used', source: 'appliance', kind: 'level', ...pctLevel,
    clearLevel: { auto: '2 points on the healthy side' }, defaults: { operator: '>', threshold: 85 } },
  { key: 'appliance_db_down', label: 'Database (ClickHouse) not answering', short: 'Database not answering', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 } },
  { key: 'appliance_backup_failed', label: 'Scheduled backup failed or overdue', short: 'Backup failed', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 } },
  { key: 'appliance_update_failed', label: 'Update failed or rolled back', short: 'Update failed', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 } },
  { key: 'appliance_license_days', label: 'Licence days left', short: 'Licence expiry', source: 'appliance', kind: 'level', unit: ' days',
    operators: ORDER_OPS, defaults: { operator: '<', threshold: 30 } },
  { key: 'appliance_time_unsynced', label: 'Clock not synchronised (NTP)', short: 'Clock not synchronised', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 } },
  { key: 'appliance_delivery_failures', label: 'Notification channel failing (failed deliveries per hour)', short: 'Failed deliveries', source: 'appliance', kind: 'level',
    unit: ' failed deliveries in the last hour', operators: ['>', '>='], min: 0, defaults: { operator: '>=', threshold: 3 }, target: 'channel',
    hint: 'Per channel. If the rule’s only channel is the failing one, the alert cannot reach anyone: give it a second channel or leave it on the default escalation policy.' },
  { key: 'appliance_sensor_offline', label: 'Remote sensor offline', short: 'Remote sensor offline', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 }, target: 'sensor' },
  { key: 'appliance_login_failures', label: 'Failed sign-ins (per 15 minutes)', short: 'Failed sign-ins', source: 'appliance', kind: 'level',
    unit: ' failed sign-ins in 15 min', operators: ['>', '>='], min: 1, defaults: { operator: '>=', threshold: 10 },
    hint: 'Per account when the typed name is an account, else per source address. Attempts refused by the sign-in rate limit are not counted.' },
  { key: 'appliance_admin_changes', label: 'Administrative changes (per 15 minutes)', short: 'Administrative changes', source: 'appliance', kind: 'event',
    unit: ' changes in 15 min', operators: ['>', '>='], min: 1, defaults: { operator: '>=', threshold: 1 }, clearLevel: false,
    hint: 'User, role, sign-in, access, key and alerting removals (see the appliance health documentation). One incident per burst of changes; further changes join it.' },
  { key: 'appliance_report_failed', label: 'Scheduled report failed', short: 'Scheduled report failed', source: 'appliance', kind: 'flag', fixed: { operator: '>', threshold: 0 },
    hint: 'One incident per enabled report schedule whose latest finished run failed or was partial. Runs waiting for a delivery retry are not judged.' },
]

const BY_KEY = new Map(METRICS.map((m) => [m.key, m]))

export function metricSpec(key: string | null | undefined): MetricSpec | undefined {
  return BY_KEY.get(String(key || ''))
}

/** Metrics a new rule of this source may pick; the current metric is kept even when retired. */
export function metricsFor(source: RuleSource, current?: string | null): MetricSpec[] {
  return METRICS.filter((m) => m.source === source && (!m.retired || m.key === current))
}

export function isRetired(key: string | null | undefined): boolean {
  return !!metricSpec(key)?.retired
}

/** The operators a condition may use: the metric's own list plus the stored one. */
export function operatorsFor(key: string, current?: string | null): string[] {
  const spec = metricSpec(key)
  const base: string[] = spec?.fixed ? [spec.fixed.operator] : [...(spec?.operators || ALL_OPERATORS)]
  const cur = current ? normalizeOperator(current) : ''
  return cur && !base.includes(cur) ? [...base, cur] : base
}

/** A fresh condition for a newly picked metric. */
export function newCondition(key: string, previous?: { operator: string; threshold: number }): { metric: string; operator: string; threshold: number; reset_threshold: null } {
  const spec = metricSpec(key)
  const start = spec?.fixed || spec?.defaults || previous || { operator: '>', threshold: 0 }
  return { metric: key, operator: start.operator, threshold: start.threshold, reset_threshold: null }
}

// ── Timers: what each evaluator honours ─────────────────────────────────────

/**
 * Which of the rule's timer fields the evaluator reads (alerting audit "What the timers really do"):
 * hold = min_duration as a breach hold; window = min_duration as the flapping window; average = min_duration
 * as the APM averaging window; silence = heartbeat silence (agent offline). clear = clear_duration as the
 * stable-recovery hold; autoclear = how long an event alert stays open with no further event.
 * cooldown false: the evaluator ignores cooldown. Since alerting plan 2.8 every family honours it (server
 * and APM metric rules delay a trigger inside the cooldown until it ends, never drop it).
 */
export type RuleTimers = {
  hold: null | 'hold' | 'window' | 'average' | 'silence'
  clear: null | 'clear' | 'autoclear'
  cooldown: boolean
  /** How the alert closes when the rule has no clear hold. */
  closes: 'clear' | 'autoclear' | 'next' | 'status' | 'expires' | 'manual' | 'none'
}

const EVENT_ALERT_METRICS = new Set(['trap', 'syslog', 'udt_new_endpoint', 'udt_rogue_endpoint', 'udt_watch_endpoint', 'udt_endpoint_moved'])

export function ruleTimers(r: { metric?: string | null; conditions?: Array<{ metric: string }> | null; service_check_id?: string | null; service_check_group_id?: string | null }): RuleTimers {
  const source = ruleSource(r)
  const metrics = (Array.isArray(r.conditions) && r.conditions.length ? r.conditions.map((c) => c.metric) : [r.metric || ''])
    .map((m) => String(m || ''))
  const m = metrics[0]
  if (EVENT_ALERT_METRICS.has(m)) return { hold: null, clear: null, cooldown: true, closes: 'expires' }
  switch (source) {
    case 'server': {
      if (m === 'host_restart' || m === 'host_shutdown') return { hold: null, clear: 'autoclear', cooldown: true, closes: 'autoclear' }
      if (m === 'host_agent_offline') return { hold: 'silence', clear: null, cooldown: true, closes: 'status' }
      if (m === 'host_service_down' || m === 'host_process_down') return { hold: null, clear: null, cooldown: true, closes: 'status' }
      // Threshold metrics (percentages, depth metrics) and automatic services: breach hold and clear hold.
      return { hold: 'hold', clear: 'clear', cooldown: true, closes: 'clear' }
    }
    case 'apm':
      if (m === 'apm_synthetic_down' || m === 'apm_slo_burn') return { hold: null, clear: null, cooldown: true, closes: 'status' }
      return { hold: 'average', clear: null, cooldown: true, closes: 'next' }
    case 'netflow': return { hold: null, clear: null, cooldown: true, closes: 'clear' }
    case 'udt': return { hold: null, clear: null, cooldown: true, closes: 'next' }
    case 'netpath':
      return { hold: null, clear: null, cooldown: true, closes: m === 'netpath_path_change' ? 'none' : 'next' }
    case 'appliance':
      if (m === 'appliance_admin_changes') return { hold: null, clear: 'autoclear', cooldown: true, closes: 'autoclear' }
      return { hold: 'hold', clear: 'clear', cooldown: true, closes: 'clear' }
    case 'service':
      if (m === 'service_status') return { hold: 'hold', clear: null, cooldown: true, closes: 'status' }
      return { hold: 'hold', clear: 'clear', cooldown: true, closes: 'clear' }
    case 'gap':
    case 'virtualization':
      return { hold: 'hold', clear: 'clear', cooldown: true, closes: 'clear' }
    default: break
  }
  // Device rules.
  if (metrics.some((x) => x === 'ping_status' || x === 'rtt' || x === 'packet_loss' || x === 'jitter')) {
    return { hold: 'hold', clear: null, cooldown: true, closes: 'status' }
  }
  if (metrics.length === 1 && (m === 'uptime_reset' || m === 'if_speed_changed')) {
    return { hold: null, clear: 'autoclear', cooldown: true, closes: 'autoclear' }
  }
  if (metrics.includes('if_flaps')) return { hold: 'window', clear: 'clear', cooldown: true, closes: 'clear' }
  return { hold: 'hold', clear: 'clear', cooldown: true, closes: 'clear' }
}

/** Default seconds of the timers the server applies when the field is empty. */
export const DEFAULT_CLEAR_S = 300
export const DEFAULT_AUTOCLEAR_S = 900
export const DEFAULT_FLAP_WINDOW_S = 900
export const APM_MIN_WINDOW_S = 300

// ── New percent rules (plan step 2.2) ────────────────────────────────────────

/** Percent metrics whose new rules start with a clear level 5 points below the limit and a 900 s clear hold. */
export const NEW_PERCENT_METRICS = new Set(['cpu', 'memory', 'if_util_pct', 'host_cpu_pct', 'host_memory_pct', 'host_swap_pct', 'host_filesystem_pct', 'host_disk_util_pct'])
export const NEW_PERCENT_CLEAR_POINTS = 5
export const NEW_PERCENT_CLEAR_S = 900

/** The clear level a new percent rule gets when none is typed, or null (not a percent rule, or out of 0–100). */
export function newPercentClearLevel(c: { metric: string; operator: string; threshold: number; reset_threshold?: number | null }): number | null {
  if (c.reset_threshold != null || !NEW_PERCENT_METRICS.has(c.metric)) return null
  const thr = Number(c.threshold)
  if (!Number.isFinite(thr)) return null
  const op = normalizeOperator(c.operator)
  if (HIGH_OPS.includes(op) && thr - NEW_PERCENT_CLEAR_POINTS >= 0) return thr - NEW_PERCENT_CLEAR_POINTS
  if ((op === '<' || op === '<=') && thr + NEW_PERCENT_CLEAR_POINTS <= 100) return thr + NEW_PERCENT_CLEAR_POINTS
  return null
}

/**
 * A new rule's conditions and clear hold with the percent defaults filled in where the operator left them
 * empty. Existing rules are never changed: callers apply this to new rules only.
 */
export function withNewPercentDefaults<C extends { metric: string; operator: string; threshold: number; reset_threshold?: number | null }>(
  conditions: C[], clearDuration: number | null,
): { conditions: C[]; clear_duration: number | null } {
  let percent = false
  const out = conditions.map((c) => {
    if (!NEW_PERCENT_METRICS.has(c.metric)) return c
    percent = true
    const level = newPercentClearLevel(c)
    return level == null ? c : { ...c, reset_threshold: level }
  })
  return { conditions: out, clear_duration: clearDuration == null && percent ? NEW_PERCENT_CLEAR_S : clearDuration }
}

// ── Validation (mirrors the server) ──────────────────────────────────────────

export type Cond = { metric: string; operator: string; threshold: number; reset_threshold?: number | null }

function healthySide(op: string, reset: number, thr: number): boolean {
  return HIGH_OPS.includes(normalizeOperator(op)) ? reset <= thr : reset >= thr
}

/**
 * Why the server would refuse this condition set, or null. `existing` are the metrics the saved rule already
 * uses (a retired key stays allowed on its own rule). Scope problems are checked by scopeProblem().
 */
export function conditionProblem(conds: Cond[], existing: Iterable<string> = []): string | null {
  const kept = new Set(existing)
  if (!conds.length) return 'Add a condition.'
  for (const c of conds) {
    const spec = metricSpec(c.metric)
    if (spec?.retired && (spec.unmapped || !kept.has(c.metric))) return spec.retired
    if (c.metric === 'tpl_') return 'Pick the template and the metric to watch, or another field.'
    const thr = Number(c.threshold)
    if (!Number.isFinite(thr)) return `${spec?.short || 'The condition'} needs a number.`
    const op = normalizeOperator(c.operator)
    if (!spec) continue
    if (spec.single && conds.length > 1) return `${spec.short} needs a rule of its own (no other conditions).`
    if (spec.fixed) continue
    if (spec.key === 'vm_alarm' && !(op === '>=' && (thr === 1 || thr === 2))) {
      return 'Use red alarms (>= 2) or red and yellow alarms (>= 1).'
    }
    if (spec.operators && !spec.operators.includes(op)) {
      return `${spec.short}: use ${spec.operators.join(', ')}.`
    }
    if (spec.min != null && thr < spec.min) return `${spec.short}: the limit must be at least ${spec.min}${unitText(spec)}.`
    if (spec.max != null && thr > spec.max) return `${spec.short}: the limit must be at most ${spec.max}${unitText(spec)}.`
    if (spec.key === 'service_response_ms' && thr <= 0) return 'The response-time limit must be above 0 ms.'
    if (c.reset_threshold != null) {
      if (spec.clearLevel === false) return `${spec.short} clears when the condition clears; it takes no clear level.`
      if (!Number.isFinite(Number(c.reset_threshold))) return 'The clear level must be a number.'
      if (!ORDER_OPS.includes(op) || !healthySide(op, Number(c.reset_threshold), thr)) {
        return 'The clear level must lie on the healthy side of the limit.'
      }
    }
  }
  return null
}

function unitText(spec: MetricSpec): string {
  return spec.unit === '%' ? '%' : (spec.unit || '').split(' in ')[0]
}

/** A vm_alarm rule fires on red only (>= 2) or red and yellow (>= 1). */
export const VM_ALARM_LEVELS = [
  { threshold: 2, label: 'Red (critical) alarms' },
  { threshold: 1, label: 'Red and yellow (warning) alarms' },
] as const

// ── Scope per family ─────────────────────────────────────────────────────────

/** Scope fields a family's rule keeps; every other scope column is sent as null. */
export const SCOPE_KEYS = ['device_id', 'group_id', 'device_type', 'location', 'scope_tag', 'service_check_id',
  'service_check_group_id', 'target', 'profile_id', 'if_role'] as const
export type ScopeKey = (typeof SCOPE_KEYS)[number]

export function scopeKeysFor(metric: string): ScopeKey[] {
  const source = ruleSource({ metric })
  if (source === 'appliance') return metricSpec(metric)?.target ? ['target'] : []
  if (source === 'virtualization') return ['target', 'device_id', 'scope_tag']
  if (metric === 'agent_no_metrics') return ['group_id', 'scope_tag']
  if (metric === 'snmp_unreachable' || metric === 'f5_cert_days') return ['device_id', 'group_id', 'device_type', 'location', 'scope_tag']
  if (SERVICE_METRICS.has(metric)) return ['service_check_id', 'service_check_group_id', 'device_id', 'scope_tag']
  if (source === 'udt') return ['device_id', 'group_id', 'device_type', 'location']
  if (source === 'netpath') return ['target']
  return [...SCOPE_KEYS]
}

// ── Payload for the families the generic builder does not know ───────────────

export type FamilyDraft = {
  source: string
  conditions: Cond[]
  min_duration: number
  clear_duration: number | null
  device_id: string
  group_id: string
  device_type: string
  location: string
  scope_tag: string
  service_check_id: string
  service_check_group_id: string
  target: string
}

const FAMILY_SOURCES = new Set(['service', 'gap', 'appliance', 'virtualization', 'udt', 'netpath'])

/**
 * The /alert-rules body for the families added after the wizard's generic builder: service-check metrics,
 * monitoring gaps, appliance, virtualisation, Device Tracker and NetPath rules, plus the single-condition
 * device metrics (F5 certificates, interface speed change). `base` is the generic payload; the condition,
 * trigger, timers and scope columns are replaced as the server's validator for the metric expects.
 */
export function familyPayload(s: FamilyDraft, base: Record<string, any>): Record<string, any> {
  const c = s.conditions[0]
  if (!c) return base
  const spec = metricSpec(c.metric)
  if (s.source === 'service' && c.metric === 'service_status') return base
  if (s.source === 'device') {
    if (s.conditions.length === 1 && c.metric === 'if_speed_changed') {
      return { ...base, operator: '==', threshold: 1, conditions: null, min_duration: 0 }
    }
    return base
  }
  if (!FAMILY_SOURCES.has(s.source)) return base
  const fixed = spec?.fixed
  const operator = fixed ? fixed.operator : normalizeOperator(c.operator)
  const threshold = fixed ? fixed.threshold : Number(c.threshold)
  const reset = !fixed && spec?.clearLevel !== false && c.reset_threshold != null ? Number(c.reset_threshold) : null
  const timers = ruleTimers({ metric: c.metric })
  const keep = new Set<string>(scopeKeysFor(c.metric))
  const scope: Record<string, any> = {}
  for (const k of SCOPE_KEYS) {
    const v = (s as any)[k]
    // '__pick__' is a scope chosen in the wizard but not picked yet (the save is refused before it is sent).
    scope[k] = keep.has(k) && typeof v === 'string' && v.trim() && v !== '__pick__' ? v.trim() : null
  }
  return {
    ...base,
    ...scope,
    metric: c.metric,
    operator,
    threshold,
    conditions: reset != null ? [{ metric: c.metric, operator, threshold, reset_threshold: reset }] : null,
    condition_logic: 'AND',
    trigger_on: 'any',
    trap_oid: null,
    min_duration: timers.hold ? Math.max(0, Math.round(Number(s.min_duration) || 0)) : 0,
    clear_duration: timers.clear ? s.clear_duration : null,
    notify_while_flapping: false,
  }
}

// ── Rule list words ──────────────────────────────────────────────────────────

function num(v: unknown): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '?'
  return Number.isInteger(n) ? n.toLocaleString('en-US') : String(Math.round(n * 100) / 100)
}

/** "5 min", "90 s", "1 h". */
export function seconds(s: number | null | undefined): string {
  const v = Math.max(0, Number(s) || 0)
  if (v >= 3600 && v % 3600 === 0) return `${v / 3600} h`
  if (v >= 60 && v % 60 === 0) return `${v / 60} min`
  return `${v} s`
}

export function limitWords(metric: string, operator: unknown, threshold: unknown): string {
  const spec = metricSpec(metric)
  const unit = spec?.unit ?? ''
  return `${OP_WORD[normalizeOperator(operator)] || normalizeOperator(operator)} ${num(threshold)}${unit}`
}

/** The template metric names and state labels the list and the wizard read from /snmp/profiles. */
export type TemplateIndex = {
  names: Record<string, string>
  units: Record<string, string>
  states: Record<string, StateInfo>
}

/** One condition in words: "Data disk used > 85%", "Interface down", "Reboot detected". */
export function conditionText(c: Cond, idx?: TemplateIndex): string {
  const m = String(c.metric || '')
  if (m.startsWith('tpl_')) {
    const key = m.slice(4)
    const name = idx?.names[key] || `${key} (template)`
    const unit = idx?.units[key]
    const u = unit ? (unit === '%' ? '%' : ` ${unit}`) : ''
    return `${name} ${OP_WORD[normalizeOperator(c.operator)] || c.operator} ${num(c.threshold)}${u}`
  }
  const spec = metricSpec(m)
  if (m === 'if_oper_status') return 'Interface down'
  if (m === 'ping_status') return Number(c.threshold) === 0 && normalizeOperator(c.operator) === '==' ? 'Device down' : `Ping status ${limitWords(m, c.operator, c.threshold)}`
  if (m === 'vm_alarm') return Number(c.threshold) >= 2 ? 'Red platform alarm' : 'Red or yellow platform alarm'
  if (spec?.fixed) return spec.short
  return `${spec?.short || m} ${limitWords(m, c.operator, c.threshold)}`
}

/** APM metric words: [label, unit, scale] (rates are stored as fractions and read as percentages). */
const APM_LABEL: Record<string, [string, string, number]> = {
  apm_latency_p50: ['p50 latency', ' ms', 1], apm_latency_p95: ['p95 latency', ' ms', 1], apm_latency_p99: ['p99 latency', ' ms', 1],
  apm_error_rate: ['Error rate', '%', 100], apm_throughput: ['Throughput', ' req/s', 1], apm_apdex: ['Apdex', '', 1],
  apm_rum_lcp_p75: ['LCP p75', ' ms', 1], apm_rum_inp_p75: ['INP p75', ' ms', 1], apm_rum_cls_p75: ['CLS p75', '', 1],
  apm_rum_error_session_rate: ['Error-affected sessions', '%', 100], apm_rum_resource_failure_rate: ['Failed resources', '%', 100],
  apm_rum_new_error_groups: ['New error groups', '', 1], apm_anomaly: ['Traffic anomaly', '', 1],
}

const TRIGGER_WORDS: Record<string, string> = { down: 'goes down', up: 'comes up', degraded: 'is degraded', any: 'changes status' }

/**
 * The rule list's trigger column: a title (what is watched) and a detail (the condition with its limit and
 * unit). Never a raw key. Server, NetFlow and APM-event rules keep their own helpers in the caller.
 */
export function triggerSummary(r: any, idx?: TemplateIndex): { title: string; detail: string } {
  const source = ruleSource(r)
  const conds: Cond[] = Array.isArray(r.conditions) && r.conditions.length
    ? r.conditions
    : [{ metric: r.metric, operator: r.operator, threshold: r.threshold }]
  const first = conds[0]
  if (source === 'trap') return { title: 'SNMP trap', detail: r.trap_oid ? `Trap OID ${r.trap_oid}` : 'Any trap' }
  if (source === 'server') return { title: r.condition_tree ? 'Combined conditions' : serverMetricLabel(r.metric), detail: serverRuleCondition(r) }
  if (r.metric === 'combined' && r.condition_tree && Array.isArray(r.condition_tree.items)) {
    return { title: 'Combined conditions', detail: treeLabel(r.subject_type === 'service_check' ? 'service_check' : 'device', r.condition_tree) }
  }
  if (source === 'apm') {
    const ev = APM_EVENT_TRIGGERS.find((x) => x.value === r.metric)
    if (ev) return { title: ev.label, detail: r.metric === 'apm_synthetic_down' ? 'After configured retries' : 'Multi-window error-budget burn' }
    const [label, unit, scale] = APM_LABEL[r.metric] || [r.metric, '', 1]
    return { title: String(r.metric).startsWith('apm_rum_') ? 'Browser RUM' : 'APM', detail: `${label} ${OP_WORD[normalizeOperator(r.operator)] || r.operator} ${num(Number(r.threshold) * scale)}${unit}` }
  }
  if (source === 'syslog') {
    return { title: 'Syslog', detail: `Severity ${limitWords('syslog', r.operator, r.threshold)}${r.target ? ` containing “${r.target}”` : ''}` }
  }
  if (r.metric === 'service_status') return { title: 'Service check', detail: `Service ${TRIGGER_WORDS[r.trigger_on] || 'changes status'}` }
  if (r.metric === 'ping_status' && conds.length === 1) {
    return { title: 'Device status', detail: `Device ${TRIGGER_WORDS[r.trigger_on || 'any'] || 'changes status'}` }
  }
  const state = stateRuleSummary(conds, idx)
  if (state) return { title: 'Template state', detail: state }
  const spec = metricSpec(first.metric)
  const title = conds.length > 1
    ? `${conds.length} conditions (${r.condition_logic === 'OR' ? 'any' : 'all'})`
    : first.metric?.startsWith('tpl_') ? 'Template metric' : SOURCE_LABEL[source] === 'Device / SNMP' ? (spec?.kind === 'status' ? 'Device status' : 'SNMP') : SOURCE_LABEL[source]
  const joiner = r.condition_logic === 'OR' ? ' or ' : ' and '
  let detail = conds.map((c) => conditionText(c, idx)).join(joiner)
  if (conds.length === 1 && first.metric === 'if_flaps') detail += ` in ${seconds(Number(r.min_duration) || DEFAULT_FLAP_WINDOW_S)}`
  if (spec?.kind === 'status' && first.metric !== 'ping_status') detail += ' (while Degraded)'
  if (spec?.retired) detail += ' — retired metric'
  if (spec?.target && r.target) detail += ` (${spec.target} ${r.target})`
  return { title, detail }
}

/** The list's hold column: "hold 5 min · clear 5 min", "window 15 min", "auto-clear 15 min", "event". */
export function timersText(r: any): string {
  const t = ruleTimers(r)
  const parts: string[] = []
  const hold = Number(r.min_duration) || 0
  if (t.hold === 'hold') parts.push(hold ? `hold ${seconds(hold)}` : 'at once')
  else if (t.hold === 'window') parts.push(`window ${seconds(hold || DEFAULT_FLAP_WINDOW_S)}`)
  else if (t.hold === 'average') parts.push(`averaged over ${seconds(Math.max(hold, APM_MIN_WINDOW_S))}`)
  else if (t.hold === 'silence') parts.push(`silent ${seconds(Math.max(hold || 180, 90))}`)
  if (t.clear === 'clear') parts.push(`clear ${r.clear_duration == null ? `${seconds(DEFAULT_CLEAR_S)} (default)` : r.clear_duration === 0 ? 'at once' : seconds(r.clear_duration)}`)
  else if (t.clear === 'autoclear') parts.push(`auto-clear ${seconds(r.clear_duration ?? DEFAULT_AUTOCLEAR_S)}`)
  if (!parts.length) parts.push(t.closes === 'expires' ? 'event · expires' : t.closes === 'next' ? 'at once' : 'event')
  return parts.join(' · ')
}

/** The list's reset words: how an alert of this rule closes and whether a notice goes out. */
export function closesText(r: any, expiry?: { enabled: boolean; label: string } | null): string {
  const t = ruleTimers(r)
  if (t.closes === 'expires') {
    return expiry && !expiry.enabled ? 'Closed by hand' : `Expires after ${expiry?.label || '7 days'} without a new event`
  }
  if (t.closes === 'none') return 'Closed by hand'
  return r.recovery_alert ? 'Auto + notify' : 'Silent'
}

// ── Routing ─────────────────────────────────────────────────────────────────

export type RouteSourceLike = 'rule' | 'legacy_levels' | 'priority_default' | 'none'

/**
 * Who a rule's alerts reach: its channels, or only an escalation route, or nobody. `route` is the escalation
 * route the server resolves (rule policy > legacy tiers > priority default > none), null when unknown.
 */
export function routingNote(channelCount: number, route: RouteSourceLike | null): { tone: 'ok' | 'muted' | 'danger'; text: string } {
  if (channelCount > 0) return { tone: 'ok', text: `${channelCount} channel${channelCount === 1 ? '' : 's'}` }
  if (route === null) return { tone: 'muted', text: 'No channel' }
  if (route === 'none') return { tone: 'danger', text: 'No channel — notifies nobody' }
  if (route === 'legacy_levels') return { tone: 'muted', text: 'No channel — escalation tiers only' }
  return { tone: 'muted', text: 'No channel — escalation policy only' }
}

// ── Template state (enum) metrics ─────────────────────────────────────────────

export type StateInfo = {
  key: string
  name: string
  groupName: string
  groupKind: string
  /** code → [text, severity] */
  labels: Record<number, [string, string]>
}

type TplMetric = { key: string; name: string; unit?: string | null; type?: string | null; labels?: Record<string, any> | null; value_map?: Record<string, any> | null }
type TplGroup = { key?: string; name?: string; kind?: string; display?: string | null; belongs_to?: unknown; children?: unknown; metrics?: TplMetric[] }

function codeOf(v: unknown): number | null {
  const n = Number(v)
  return Number.isFinite(n) && Number.isInteger(n) ? n : null
}

/** Names, units and enum value maps over the given templates (first definition of a key wins). */
export function templateIndex(templates: Array<{ oid_groups?: TplGroup[] | null }>): TemplateIndex {
  const out: TemplateIndex = { names: {}, units: {}, states: {} }
  for (const t of templates || []) {
    for (const g of t.oid_groups || []) {
      for (const m of g.metrics || []) {
        if (!m?.key) continue
        if (!(m.key in out.names)) out.names[m.key] = m.name || m.key
        if (m.unit && !(m.key in out.units)) out.units[m.key] = m.unit
        if (m.type === 'enum' && !(m.key in out.states)) {
          const labels: Record<number, [string, string]> = {}
          for (const [code, spec] of Object.entries(m.labels || {})) {
            const c = codeOf(code)
            if (c == null) continue
            labels[c] = spec && typeof spec === 'object' ? [String(spec.text ?? code), String(spec.sev ?? '')] : [String(spec), '']
          }
          for (const [text, code] of Object.entries(m.value_map || {})) {
            const c = codeOf(code)
            if (c != null && !(c in labels)) labels[c] = [text, '']
          }
          if (Object.keys(labels).length) {
            out.states[m.key] = { key: m.key, name: m.name || m.key, groupName: g.name || g.key || '', groupKind: g.kind || 'scalar', labels }
          }
        }
      }
    }
  }
  return out
}

export function knownCodes(info: StateInfo): number[] {
  return Object.keys(info.labels).map(Number).sort((a, b) => a - b)
}

/** template_states.bad_codes: the crit codes, else all-but-the-one-ok when the rest are warn, else none. */
export function badCodes(info: StateInfo): number[] {
  const codes = knownCodes(info)
  const crit = codes.filter((c) => info.labels[c][1] === 'crit')
  if (crit.length) return crit
  const ok = codes.filter((c) => info.labels[c][1] === 'ok')
  const rest = codes.filter((c) => info.labels[c][1] !== 'ok')
  if (ok.length === 1 && rest.length && rest.every((c) => info.labels[c][1] === 'warn')) return rest
  return []
}

export const MAX_STATE_CONDITIONS = 4

/** template_states.conditions_for: AND-combined conditions that breach exactly on `bad`; null when more than four. */
export function conditionsForStates(metric: string, info: StateInfo, bad: number[]): Cond[] | null {
  const codes = knownCodes(info)
  const set = [...new Set(bad)].sort((a, b) => a - b)
  if (!set.length || !codes.length) return null
  if (set.length === 1) return [{ metric, operator: '==', threshold: set[0] }]
  const good = codes.filter((c) => !set.includes(c))
  if (good.length === 1) return [{ metric, operator: '!=', threshold: good[0] }]
  const lo = set[0]
  const hi = set[set.length - 1]
  const out: Cond[] = []
  if (lo > codes[0]) out.push({ metric, operator: '>=', threshold: lo })
  if (hi < codes[codes.length - 1]) out.push({ metric, operator: '<=', threshold: hi })
  for (const c of codes) if (c >= lo && c <= hi && !set.includes(c)) out.push({ metric, operator: '!=', threshold: c })
  return out.length > 0 && out.length <= MAX_STATE_CONDITIONS ? out : null
}

/** The known codes the AND-combined conditions breach on. */
export function statesOf(conds: Cond[], info: StateInfo): number[] {
  return knownCodes(info).filter((code) => conds.every((c) => cmp(code, c.operator, Number(c.threshold))))
}

/** The state metric a condition set is on (every condition on one enum metric with a value map), or null. */
export function stateRuleInfo(conds: Cond[] | null | undefined, idx?: TemplateIndex): StateInfo | null {
  if (!idx || !conds?.length) return null
  const m = String(conds[0].metric || '')
  if (!m.startsWith('tpl_') || conds.some((c) => c.metric !== m)) return null
  const info = idx.states[m.slice(4)]
  if (!info) return null
  return conds.every((c) => ['==', '!=', '>=', '<=', '>', '<'].includes(normalizeOperator(c.operator))) ? info : null
}

function joinOr(words: string[]): string {
  if (words.length <= 1) return words.join('')
  return `${words.slice(0, -1).join(', ')} or ${words[words.length - 1]}`
}

/** "Down", "not Established", "Critical, Shutdown or Not Functioning" for the bad codes. */
export function stateWords(info: StateInfo, bad: number[]): string {
  const codes = knownCodes(info)
  const good = codes.filter((c) => !bad.includes(c))
  if (bad.length > 1 && good.length === 1) return `not ${info.labels[good[0]][0]}`
  return joinOr(bad.map((c) => info.labels[c]?.[0] ?? String(c)))
}

/** "IPsec Tunnels: Status is Down" for a state rule, or null when the conditions are not one. */
export function stateRuleSummary(conds: Cond[], idx?: TemplateIndex): string | null {
  const info = stateRuleInfo(conds, idx)
  if (!info) return null
  const bad = statesOf(conds, info)
  if (!bad.length) return `${info.name}: no known state matches`
  const generic = ['status', 'state'].includes(info.name.toLowerCase())
  const base = info.groupKind === 'table' || generic ? `${info.groupName}: ${info.name}` : info.name
  return `${base} is ${stateWords(info, bad)}`
}

// ── Recommended default rules (GET /alert-rules/recommended) ─────────────────

export type DefaultRecommendation = {
  key: string
  family: 'device' | 'service' | 'server' | 'trap' | 'syslog'
  name: string
  description: string
  metric: string
  operator: string
  threshold: number
  trigger_on: string
  trap_oid?: string | null
  min_duration: number
  clear_duration: number | null
  severity: 'info' | 'warning' | 'critical'
  priority: string | null
  recovery_alert: boolean
  cooldown: number
  status: 'available' | 'accepted' | 'covered'
  rule: { id: string; name: string; enabled: boolean } | null
  covered_by: { id: string; name: string } | null
}

/** Each recommendation with its family, condition and timers in the rule list's words; available ones first. */
export function recommendationRows(items: DefaultRecommendation[]): Array<{ item: DefaultRecommendation; family: string; condition: string; timers: string }> {
  const order = { available: 0, covered: 1, accepted: 2 } as const
  return [...items]
    .map((item, i) => ({ item, i }))
    .sort((a, b) => order[a.item.status] - order[b.item.status] || a.i - b.i)
    .map(({ item }) => {
      const rule = { ...item, conditions: null, service_check_id: null }
      const t = triggerSummary(rule)
      return { item, family: SOURCE_LABEL[ruleSource(rule)], condition: t.detail, timers: timersText(rule) }
    })
}
