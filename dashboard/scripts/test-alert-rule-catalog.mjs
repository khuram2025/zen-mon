// Alert rule catalogue (src/components/forms/alertRuleCatalog.ts): every rule family the server accepts is
// offered with the inputs and limits its validator enforces, retired keys stay readable but are not offered,
// stored word operators open as symbols, the payload carries only the scope a family takes, new percent rules
// get the 5-point / 900 s clear defaults, template state rules read as "Status is Down", and the rule list
// describes every family in words (never a raw key), with its timers, routing and last-fired time.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const src = path.join(process.cwd(), 'src')
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
async function load(contents) {
  const out = await build({
    stdin: { contents, resolveDir: process.cwd(), loader: 'ts' },
    bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022',
    plugins: [aliasPlugin], logLevel: 'silent',
  })
  return import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)
}

const C = await load(`export * from './src/components/forms/alertRuleCatalog'`)

// ── Families and operators ──────────────────────────────────────────────────
const fam = (metric, extra = {}) => C.ruleSource({ metric, ...extra })
assert.equal(fam('appliance_disk_pct'), 'appliance')
assert.equal(fam('vm_datastore_pct'), 'virtualization')
assert.equal(fam('snmp_unreachable'), 'gap')
assert.equal(fam('agent_no_metrics'), 'gap', 'saved through /alert-rules, not the host rule API')
assert.equal(fam('udt_rogue_endpoint'), 'udt')
assert.equal(fam('netpath_rtt'), 'netpath')
assert.equal(fam('service_tls_days'), 'service')
assert.equal(fam('f5_cert_days'), 'device', 'F5 certificates are device-scoped')
assert.equal(fam('host_cpu_pct'), 'server')
assert.equal(fam('if_flaps'), 'device')
for (const s of ['device', 'server', 'service', 'gap', 'virtualization', 'udt', 'netpath', 'appliance', 'trap', 'syslog', 'apm', 'netflow']) {
  assert(C.RULE_SOURCES.some((x) => x.value === s), `the wizard and the family filter offer ${s}`)
}
assert.equal(C.normalizeOperator('gt'), '>')
assert.equal(C.normalizeOperator('GTE'), '>=')
assert.equal(C.normalizeOperator('<='), '<=')
assert.deepEqual(C.operatorsFor('if_flaps'), ['>', '>='])
assert.deepEqual(C.operatorsFor('if_speed_changed'), ['=='])
assert.deepEqual(C.operatorsFor('snmp_unreachable'), ['>', '>='])
assert.deepEqual(C.operatorsFor('service_tls_days'), ['>', '>=', '<', '<='])
assert(C.operatorsFor('packet_loss', 'gt').includes('>'), 'a stored "gt" is shown as ">" (the shipped Packet Loss rule)')
assert(C.operatorsFor('netpath_rtt', '==').includes('=='), 'a stored operator outside the list stays visible')

// ── Retired keys and labels ─────────────────────────────────────────────────
const deviceKeys = C.metricsFor('device').map((m) => m.key)
for (const k of ['session_count', 'jitter', 'fan_state', 'psu_state', 'vpn_tunnel_state', 'ha_state', 'bgp_neighbor_down']) {
  assert(!deviceKeys.includes(k), `${k} is not offered for new rules`)
  assert(C.metricsFor('device', k).some((m) => m.key === k), `${k} still shows on a rule that uses it`)
  assert(C.isRetired(k))
}
for (const k of ['if_error_rate_pct', 'if_discard_rate_pct', 'if_flaps', 'if_speed_changed', 'f5_cert_days', 'temperature']) assert(deviceKeys.includes(k))
assert.equal(C.metricSpec('temperature').label, 'Device temperature (per sensor)')
assert.deepEqual(C.metricsFor('appliance').map((m) => m.key).sort(), ['appliance_admin_changes', 'appliance_backup_failed', 'appliance_db_down',
  'appliance_delivery_failures', 'appliance_disk_pct', 'appliance_license_days', 'appliance_login_failures', 'appliance_report_failed',
  'appliance_sensor_offline', 'appliance_time_unsynced', 'appliance_update_failed'], 'every appliance metric (appliance_alert_service.APPLIANCE_METRICS)')
assert.equal(C.metricSpec('appliance_delivery_failures').target, 'channel')
assert.equal(C.metricSpec('appliance_sensor_offline').target, 'sensor')
assert.match(C.metricSpec('rtt').hint, /only when the device’s confirmed status is Degraded/)
assert.match(C.metricSpec('packet_loss').hint, /only when the device’s confirmed status is Degraded/)

// ── Validators (alert_rules.py) ─────────────────────────────────────────────
const P = (conds, existing) => C.conditionProblem(conds, existing)
assert.match(P([{ metric: 'service_tls_days', operator: '==', threshold: 30 }]), /use >, >=, <, <=/)
assert.equal(P([{ metric: 'service_tls_days', operator: '<', threshold: 30 }]), null)
assert.match(P([{ metric: 'service_tls_days', operator: '<', threshold: 5000 }]), /at most 3650/)
assert.match(P([{ metric: 'service_response_ms', operator: '>', threshold: 0 }]), /above 0 ms/)
assert.match(P([{ metric: 'service_tls_days', operator: '<', threshold: 30, reset_threshold: 20 }]), /healthy side/)
assert.equal(P([{ metric: 'service_tls_days', operator: '<', threshold: 30, reset_threshold: 35 }]), null)
assert.match(P([{ metric: 'vm_datastore_pct', operator: '>', threshold: 120 }]), /at most 100/)
assert.match(P([{ metric: 'vm_alarm', operator: '>=', threshold: 3 }]), /red alarms/)
assert.equal(P([{ metric: 'vm_alarm', operator: '>=', threshold: 1 }]), null)
assert.match(P([{ metric: 'snmp_unreachable', operator: '>=', threshold: 0 }]), /at least 1/)
assert.match(P([{ metric: 'snmp_unreachable', operator: '>=', threshold: 20000 }]), /at most 10080/)
assert.match(P([{ metric: 'snmp_unreachable', operator: '<', threshold: 5 }]), /use >, >=/)
assert.match(P([{ metric: 'agent_no_metrics', operator: '>', threshold: 5, reset_threshold: 2 }]), /takes no clear level/)
assert.match(P([{ metric: 'if_flaps', operator: '<', threshold: 5 }]), /use >, >=/)
assert.match(P([{ metric: 'if_flaps', operator: '>=', threshold: 5 }, { metric: 'cpu', operator: '>', threshold: 90 }]), /rule of its own/)
assert.match(P([{ metric: 'appliance_disk_pct', operator: '>', threshold: 85, reset_threshold: 90 }]), /healthy side/)
assert.match(P([{ metric: 'jitter', operator: '>', threshold: 30 }]), /does not measure jitter|never fires/)
assert.equal(P([{ metric: 'jitter', operator: '>', threshold: 30 }], ['jitter']), null, 'an existing jitter rule stays editable')
assert.match(P([{ metric: 'ha_state', operator: '==', threshold: 1 }], ['ha_state']), /No collector/, 'unmapped keys are refused even on old rules')
assert.equal(P([{ metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1 }]), null)

// ── Timers each evaluator honours ───────────────────────────────────────────
assert.equal(C.ruleTimers({ metric: 'host_cpu_pct' }).cooldown, true, 'server agent rules enforce cooldown (plan 2.8: delayed, not dropped)')
assert.equal(C.ruleTimers({ metric: 'apm_latency_p95' }).cooldown, true, 'APM metric rules enforce cooldown (plan 2.8)')
assert.deepEqual([C.ruleTimers({ metric: 'host_auto_service_stopped' }).hold, C.ruleTimers({ metric: 'host_auto_service_stopped' }).clear], ['hold', 'clear'])
assert.equal(C.timersText({ metric: 'host_fs_days_left', min_duration: 3600, clear_duration: 3600 }), 'hold 1 h · clear 1 h')
assert.equal(C.ruleTimers({ metric: 'apm_latency_p95' }).hold, 'average')
assert.equal(C.ruleTimers({ metric: 'apm_synthetic_down' }).cooldown, true)
assert.equal(C.ruleTimers({ metric: 'ping_status' }).clear, null, 'status rules have no clear hold')
assert.equal(C.ruleTimers({ metric: 'uptime_reset' }).hold, null, 'events have no breach hold')
assert.equal(C.ruleTimers({ metric: 'if_speed_changed' }).clear, 'autoclear')
assert.equal(C.ruleTimers({ metric: 'if_flaps' }).hold, 'window')
assert.equal(C.ruleTimers({ metric: 'udt_new_endpoint' }).closes, 'expires')
assert.equal(C.ruleTimers({ metric: 'trap' }).closes, 'expires')
assert.equal(C.ruleTimers({ metric: 'netpath_rtt' }).hold, null)
assert.equal(C.ruleTimers({ metric: 'appliance_admin_changes' }).clear, 'autoclear')

// ── Payloads per family ─────────────────────────────────────────────────────
const blank = { device_id: '', group_id: '', device_type: '', location: '', scope_tag: '', service_check_id: '', service_check_group_id: '', target: '' }
const base = { name: 'x', metric: 'whatever', operator: '==', threshold: 0, device_id: 'stale', group_id: 'stale', trigger_on: 'down', conditions: [{}] }
let pl = C.familyPayload({ ...blank, source: 'appliance', conditions: [{ metric: 'appliance_disk_pct', operator: '>', threshold: 85 }], min_duration: 600, clear_duration: null, target: 'x', device_id: 'd' }, base)
assert.deepEqual([pl.metric, pl.operator, pl.threshold, pl.trigger_on, pl.min_duration, pl.clear_duration], ['appliance_disk_pct', '>', 85, 'any', 600, null])
for (const k of C.SCOPE_KEYS) assert.equal(pl[k], null, `appliance disk: no ${k}`)
assert.equal(pl.conditions, null)
pl = C.familyPayload({ ...blank, source: 'appliance', conditions: [{ metric: 'appliance_delivery_failures', operator: '>=', threshold: 3 }], min_duration: 0, clear_duration: null, target: 'ch-1' }, base)
assert.equal(pl.target, 'ch-1', 'a channel rule keeps its channel')
pl = C.familyPayload({ ...blank, source: 'appliance', conditions: [{ metric: 'appliance_db_down', operator: '<', threshold: 7 }], min_duration: 120, clear_duration: null }, base)
assert.deepEqual([pl.operator, pl.threshold], ['>', 0], 'a flag metric stores its fixed condition')
pl = C.familyPayload({ ...blank, source: 'virtualization', conditions: [{ metric: 'vm_datastore_pct', operator: '>', threshold: 85, reset_threshold: 80 }], min_duration: 0, clear_duration: 600, target: 'conn-1', group_id: 'g' }, base)
assert.equal(pl.target, 'conn-1'); assert.equal(pl.group_id, null)
assert.deepEqual(pl.conditions, [{ metric: 'vm_datastore_pct', operator: '>', threshold: 85, reset_threshold: 80 }])
assert.equal(pl.clear_duration, 600)
pl = C.familyPayload({ ...blank, source: 'gap', conditions: [{ metric: 'agent_no_metrics', operator: '>=', threshold: 15 }], min_duration: 0, clear_duration: null, group_id: 'g', scope_tag: 'prod', device_id: 'd' }, base)
assert.deepEqual([pl.group_id, pl.scope_tag, pl.device_id, pl.target], ['g', 'prod', null, null])
pl = C.familyPayload({ ...blank, source: 'gap', conditions: [{ metric: 'snmp_unreachable', operator: '>=', threshold: 15 }], min_duration: 0, clear_duration: null, location: 'DC1', target: 'x' }, base)
assert.deepEqual([pl.location, pl.target], ['DC1', null])
pl = C.familyPayload({ ...blank, source: 'service', conditions: [{ metric: 'service_tls_days', operator: '<', threshold: 30 }], min_duration: 0, clear_duration: null, device_id: 'd', group_id: 'g' }, base)
assert.deepEqual([pl.device_id, pl.group_id, pl.metric], ['d', null, 'service_tls_days'])
pl = C.familyPayload({ ...blank, source: 'service', conditions: [{ metric: 'service_status', operator: '==', threshold: 0 }], min_duration: 0, clear_duration: null }, base)
assert.equal(pl, base, 'a service status rule keeps the generic payload')
pl = C.familyPayload({ ...blank, source: 'netpath', conditions: [{ metric: 'netpath_rtt', operator: '>', threshold: 150 }], min_duration: 300, clear_duration: 300, target: 'Office-365' }, base)
assert.deepEqual([pl.target, pl.min_duration, pl.clear_duration], ['Office-365', 0, null], 'NetPath rules have no hold or clear hold')
pl = C.familyPayload({ ...blank, source: 'udt', conditions: [{ metric: 'udt_rogue_endpoint', operator: '>', threshold: 0 }], min_duration: 60, clear_duration: null, group_id: 'g', scope_tag: 't' }, base)
assert.deepEqual([pl.operator, pl.threshold, pl.min_duration, pl.group_id, pl.scope_tag], ['==', 1, 0, 'g', null])
pl = C.familyPayload({ ...blank, source: 'device', conditions: [{ metric: 'if_speed_changed', operator: '>', threshold: 0 }], min_duration: 300, clear_duration: 900 }, { ...base, clear_duration: 900 })
assert.deepEqual([pl.operator, pl.threshold, pl.min_duration, pl.clear_duration], ['==', 1, 0, 900])

// ── New percent rules: clear 5 points below, after 900 s (plan step 2.2) ─────
let d = C.withNewPercentDefaults([{ metric: 'cpu', operator: '>', threshold: 90, reset_threshold: null }], null)
assert.equal(d.conditions[0].reset_threshold, 85); assert.equal(d.clear_duration, 900)
d = C.withNewPercentDefaults([{ metric: 'host_memory_pct', operator: 'gt', threshold: 95, reset_threshold: null }], 120)
assert.equal(d.conditions[0].reset_threshold, 90); assert.equal(d.clear_duration, 120, 'a typed clear hold is kept')
d = C.withNewPercentDefaults([{ metric: 'cpu', operator: '>', threshold: 90, reset_threshold: 70 }], null)
assert.equal(d.conditions[0].reset_threshold, 70, 'a typed clear level is kept')
d = C.withNewPercentDefaults([{ metric: 'if_errors', operator: '>', threshold: 100, reset_threshold: null }], null)
assert.equal(d.conditions[0].reset_threshold, null); assert.equal(d.clear_duration, null, 'not a percent rule: unchanged')
d = C.withNewPercentDefaults([{ metric: 'memory', operator: '<', threshold: 10, reset_threshold: null }], null)
assert.equal(d.conditions[0].reset_threshold, 15)
assert.equal(C.newPercentClearLevel({ metric: 'cpu', operator: '>', threshold: 3 }), null, 'never below 0')

// ── Template state rules ────────────────────────────────────────────────────
const tpl = [{ oid_groups: [
  { key: 'fgt_tunnels', name: 'IPsec Tunnels', kind: 'table', metrics: [{ key: 'fgt_tun_status', name: 'Status', type: 'enum',
    labels: { 1: { text: 'Down', sev: 'crit' }, 2: { text: 'Up', sev: 'ok' } } }] },
  { key: 'cisco_psu', name: 'Power Supplies', kind: 'table', metrics: [{ key: 'cisco_psu_state', name: 'State', type: 'enum',
    labels: { 1: { text: 'Normal', sev: 'ok' }, 2: { text: 'Warning', sev: 'warn' }, 3: { text: 'Critical', sev: 'crit' }, 4: { text: 'Shutdown', sev: 'crit' },
      5: { text: 'Not Present', sev: 'info' }, 6: { text: 'Not Functioning', sev: 'crit' } } }] },
  { key: 'jnx_bgp', name: 'BGP Peers', kind: 'table', metrics: [{ key: 'jnx_bgp_state', name: 'State', type: 'enum',
    labels: { 1: { text: 'Idle', sev: 'warn' }, 2: { text: 'Connect', sev: 'warn' }, 3: { text: 'Active', sev: 'warn' }, 4: { text: 'OpenSent', sev: 'warn' },
      5: { text: 'OpenConfirm', sev: 'warn' }, 6: { text: 'Established', sev: 'ok' } } }] },
  { key: 'sys', name: 'System', kind: 'scalar', metrics: [{ key: 'fgt_cpu', name: 'CPU', unit: '%', type: 'gauge' }] },
] }]
const idx = C.templateIndex(tpl)
assert.equal(idx.names.fgt_cpu, 'CPU'); assert.equal(idx.units.fgt_cpu, '%')
const psu = idx.states.cisco_psu_state
assert.deepEqual(C.badCodes(psu), [3, 4, 6])
// template_states.conditions_for: ">= 3" AND "!= 5" (an empty bay never pages).
assert.deepEqual(C.conditionsForStates('tpl_cisco_psu_state', psu, [3, 4, 6]),
  [{ metric: 'tpl_cisco_psu_state', operator: '>=', threshold: 3 }, { metric: 'tpl_cisco_psu_state', operator: '!=', threshold: 5 }])
assert.deepEqual(C.conditionsForStates('tpl_jnx_bgp_state', idx.states.jnx_bgp_state, C.badCodes(idx.states.jnx_bgp_state)),
  [{ metric: 'tpl_jnx_bgp_state', operator: '!=', threshold: 6 }])
assert.deepEqual(C.conditionsForStates('tpl_fgt_tun_status', idx.states.fgt_tun_status, [1]), [{ metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1 }])
assert.deepEqual(C.statesOf([{ metric: 'tpl_cisco_psu_state', operator: '>=', threshold: 3 }, { metric: 'tpl_cisco_psu_state', operator: '!=', threshold: 5 }], psu), [3, 4, 6])
assert.equal(C.stateRuleSummary([{ metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1 }], idx), 'IPsec Tunnels: Status is Down')
assert.equal(C.stateRuleSummary([{ metric: 'tpl_cisco_psu_state', operator: '>=', threshold: 3 }, { metric: 'tpl_cisco_psu_state', operator: '!=', threshold: 5 }], idx),
  'Power Supplies: State is Critical, Shutdown or Not Functioning')
assert.equal(C.stateRuleSummary([{ metric: 'tpl_jnx_bgp_state', operator: '!=', threshold: 6 }], idx), 'BGP Peers: State is not Established')
assert.equal(C.stateRuleSummary([{ metric: 'tpl_fgt_cpu', operator: '>', threshold: 90 }], idx), null, 'a gauge is not a state rule')

// ── Rule list words ─────────────────────────────────────────────────────────
const T = (r) => C.triggerSummary(r, idx)
const RAW = /\b(if_util_pct|uptime_reset|appliance_disk_pct|if_flaps|vm_alarm|snmp_unreachable|service_tls_days|udt_[a-z_]+|netpath_[a-z_]+|tpl_[a-z_]+)\b/
const words = [
  [{ metric: 'if_util_pct', operator: '>', threshold: 80 }, 'Interface utilisation > 80%'],
  [{ metric: 'uptime_reset', operator: '==', threshold: 1 }, 'Reboot'],
  [{ metric: 'appliance_disk_pct', operator: '>', threshold: 85 }, 'Data disk used > 85%'],
  [{ metric: 'appliance_license_days', operator: '<', threshold: 30 }, 'Licence expiry < 30 days'],
  [{ metric: 'service_tls_days', operator: '<', threshold: 30 }, 'Certificate expiry < 30 days'],
  [{ metric: 'service_response_ms', operator: '>', threshold: 1500 }, 'Response time > 1,500 ms'],
  [{ metric: 'f5_cert_days', operator: 'lt', threshold: 7 }, 'F5 certificate expiry < 7 days'],
  [{ metric: 'snmp_unreachable', operator: '>=', threshold: 15 }, 'SNMP polls unanswered ≥ 15 min'],
  [{ metric: 'vm_alarm', operator: '>=', threshold: 2 }, 'Red platform alarm'],
  [{ metric: 'vm_host_disconnected', operator: '==', threshold: 1 }, 'Host disconnected'],
  [{ metric: 'udt_rogue_endpoint', operator: '==', threshold: 1 }, 'Rogue endpoint'],
  [{ metric: 'netpath_loss', operator: '>', threshold: 5 }, 'Path packet loss > 5%'],
  [{ metric: 'if_error_rate_pct', operator: '>', threshold: 1 }, 'Interface error rate > 1% of packets'],
  [{ metric: 'if_speed_changed', operator: '==', threshold: 1 }, 'Interface speed change'],
  [{ metric: 'if_flaps', operator: '>=', threshold: 5, min_duration: 0 }, 'Interface state changes ≥ 5 changes in 15 min'],
  [{ metric: 'packet_loss', operator: 'gt', threshold: 5 }, 'Packet loss > 5% (while Degraded)'],
  [{ metric: 'tpl_fgt_cpu', operator: '>', threshold: 90 }, 'CPU > 90%'],
  [{ metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1 }, 'IPsec Tunnels: Status is Down'],
  [{ metric: 'temperature', operator: '>', threshold: 70 }, 'Temperature > 70 °C'],
  [{ metric: 'appliance_delivery_failures', operator: '>=', threshold: 3, target: 'ch-1' }, 'Failed deliveries ≥ 3 failed deliveries in the last hour (channel ch-1)'],
]
for (const [r, want] of words) {
  const t = T(r)
  assert.equal(t.detail, want)
  assert(!RAW.test(`${t.title} ${t.detail}`), `no raw key in "${t.title}: ${t.detail}"`)
}
assert.equal(T({ metric: 'ping_status', operator: '==', threshold: 0, trigger_on: 'down' }).detail, 'Device goes down')
assert.equal(T({ metric: 'service_status', trigger_on: 'degraded' }).detail, 'Service is degraded')
assert.equal(T({ metric: 'trap', trap_oid: '1.3.6.1.6.3.1.1.5.1' }).detail, 'Trap OID 1.3.6.1.6.3.1.1.5.1')
assert.equal(T({ metric: 'host_cpu_pct', operator: 'gt', threshold: 90 }).detail, 'CPU usage > 90%')
assert.equal(T({ metric: 'apm_error_rate', operator: '>', threshold: 0.02 }).detail, 'Error rate > 2%')
assert.match(T({ metric: 'session_count', operator: '>', threshold: 1000 }).detail, /retired metric/)
const multi = T({ metric: 'cpu', condition_logic: 'OR', conditions: [{ metric: 'cpu', operator: '>', threshold: 90 }, { metric: 'memory', operator: '>', threshold: 95 }] })
assert.equal(multi.detail, 'CPU > 90% or Memory > 95%')

assert.equal(C.timersText({ metric: 'ping_status', min_duration: 180 }), 'hold 3 min')
assert.equal(C.timersText({ metric: 'cpu', min_duration: 300, clear_duration: null }), 'hold 5 min · clear 5 min (default)')
assert.equal(C.timersText({ metric: 'cpu', min_duration: 0, clear_duration: 0 }), 'at once · clear at once')
assert.equal(C.timersText({ metric: 'if_flaps', min_duration: 0, clear_duration: null }), 'window 15 min · clear 5 min (default)')
assert.equal(C.timersText({ metric: 'uptime_reset', clear_duration: null }), 'auto-clear 15 min')
assert.equal(C.timersText({ metric: 'trap' }), 'event · expires')
assert.equal(C.timersText({ metric: 'apm_latency_p95', min_duration: 60 }), 'averaged over 5 min')
assert.equal(C.timersText({ metric: 'host_agent_offline', min_duration: 0 }), 'silent 3 min')
assert.equal(C.timersText({ metric: 'netpath_rtt' }), 'at once')
assert.equal(C.closesText({ metric: 'syslog' }, { enabled: true, label: '7 days' }), 'Expires after 7 days without a new event')
assert.equal(C.closesText({ metric: 'syslog' }, { enabled: false, label: '7 days' }), 'Closed by hand')
assert.equal(C.closesText({ metric: 'cpu', recovery_alert: true }), 'Auto + notify')

assert.deepEqual(C.routingNote(0, 'none'), { tone: 'danger', text: 'No channel — notifies nobody' })
assert.equal(C.routingNote(0, 'priority_default').tone, 'muted')
assert.equal(C.routingNote(2, 'none').text, '2 channels')
assert.equal(C.routingNote(0, null).tone, 'muted', 'never "nobody" while the policies are unknown')

// ── Recommended default rules ────────────────────────────────────────────────
const rec = (key, status, metric, extra = {}) => ({ key, status, metric, family: 'device', name: key, description: '', operator: '>', threshold: 1,
  trigger_on: 'any', min_duration: 0, clear_duration: null, severity: 'warning', priority: 'P3', recovery_alert: true, cooldown: 0, rule: null, covered_by: null, ...extra })
const rows = C.recommendationRows([
  rec('a', 'accepted', 'cpu', { rule: { id: 'r1', name: 'CPU', enabled: true } }),
  rec('b', 'covered', 'service_status', { trigger_on: 'down', covered_by: { id: 'r2', name: 'Service Down' } }),
  rec('c', 'available', 'host_disk_util_pct', { operator: 'gt', threshold: 90, min_duration: 900, clear_duration: 900 }),
])
assert.deepEqual(rows.map((r) => r.item.key), ['c', 'b', 'a'], 'available first, then covered, then accepted')
assert.equal(rows[0].family, 'Server'); assert.equal(rows[0].condition, 'Disk I/O utilization > 90%'); assert.equal(rows[0].timers, 'hold 15 min · clear 15 min')
assert.equal(rows[1].condition, 'Service goes down')

// ── Wizard and list wiring (source) ─────────────────────────────────────────
const wiz = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
const fields = fs.readFileSync('src/components/forms/AlertRuleFamilyFields.tsx', 'utf8')
const page = fs.readFileSync('src/pages/AlertRulesPage.tsx', 'utf8')
assert(wiz.includes('RULE_SOURCES.map('), 'the wizard offers every family')
assert(wiz.includes("metricsFor('device', c.metric)"), 'the device field list comes from the catalogue (no retired keys for new rules)')
assert(!/<SelectItem value="jitter">/.test(wiz) && !/session_count', label/.test(wiz), 'no hard-coded retired keys in the wizard')
assert(wiz.includes("operator: symbol(c.operator)"), 'stored word operators open as symbols')
assert(!/Events require manual resolution|Event alerts require manual resolution/.test(wiz), 'the trap/syslog hint no longer says manual resolution')
assert(wiz.includes('Event alert expiry'), 'event alerts explain the expiry window')
assert(wiz.includes('<TemplateVars vars={templateVars} />') && wiz.includes('templateDefaultsMap?.variables?.[templateKind]'), 'placeholders follow the server list per kind')
assert(!/\{TEMPLATE_VARS\.map/.test(wiz), 'no fixed placeholder list offered to every rule')
assert(wiz.includes('withNewPercentDefaults(') && wiz.includes('!isEdit'), 'percent defaults apply to new rules only')
assert(wiz.includes('{timers.cooldown ? ('), 'cooldown is shown where the evaluator honours it (now every family)')
assert(wiz.includes('label="Window"'), 'the flapping hold is labelled as the window')
assert(wiz.includes('<StatePicker'), 'template state metrics get the state picker')
assert(fields.includes("'/virtualization/connectors'") && fields.includes("'/netpath/probes'") && fields.includes("'/sensors'"), 'pickers for connectors, probes and sensors')
assert(page.includes('triggerSummary(r, tplIndex)') && page.includes('timersText(r)') && page.includes('<RuleNotifies') && page.includes('last_triggered_at'), 'the list shows trigger words, timers, routing and last fired')
assert(page.includes('<RecommendedRulesPanel'), 'the Recommended tab')
assert(!/font-mono text-2xs text-muted">\s*\{r\.metric === 'trap'/.test(page), 'no raw metric key column')

// ── Template page "Recommended alerts": state items and a changed state set ──
{
  const TM = await load(`export * from './src/components/alerts/thresholdModel'`)
  assert.equal(TM.changeWords({ field: 'states', from: ['Down'], to: ['Down', 'Negotiating'] }), 'alerting states Down → Down, Negotiating')
  assert.equal(TM.changeWords({ field: 'states', from: [1], to: [1, 3] }), 'alerting states 1 → 1, 3')
  const recPage = fs.readFileSync('src/pages/templates/RecommendedAlerts.tsx', 'utf8')
  assert(recPage.includes("if (it.source === 'state' && it.condition_label) return it.condition_label"), 'state items read as their label')
  assert(recPage.includes('storedConditions(it, meta)'), 'and show the conditions they are stored as')
  assert(recPage.includes("if (c.field !== 'states') return changeWords(c)"), 'a changed state set is named with the template labels')
  assert(recPage.includes("it.source === 'state' ? 'from states'"))
}

// ── The wizard's own ruleToState / stateToPayload (sliced like test-network-contracts.mjs) ──
{
  const start = wiz.indexOf('const TEMPLATE_FIELDS =')
  const end = wiz.indexOf('export function AlertRuleWizardDialog(')
  const W = await load(`import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
import { isApmEvent } from './src/components/forms/apmEventTriggers';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status','if_error_rate_pct','if_discard_rate_pct','if_flaps','if_speed_changed']);
${wiz.slice(start, end)}
export { ruleToState, stateToPayload };`)
  // The shipped Packet Loss rule stores "gt": it opens with ">" selected, not an empty Operator field.
  const loss = W.ruleToState({ name: 'Packet Loss', metric: 'packet_loss', operator: 'gt', threshold: 5, trigger_on: 'any' })
  assert.equal(loss.source, 'device'); assert.equal(loss.conditions[0].operator, '>')
  const compound = W.ruleToState({ name: 'x', metric: 'cpu', conditions: [{ metric: 'cpu', operator: 'gte', threshold: 90 }] })
  assert.equal(compound.conditions[0].operator, '>=')
  const opens = (metric, extra = {}) => W.ruleToState({ name: 'x', metric, operator: '>', threshold: 1, ...extra }).source
  assert.equal(opens('appliance_disk_pct'), 'appliance')
  assert.equal(opens('vm_alarm'), 'virtualization')
  assert.equal(opens('agent_no_metrics'), 'gap')
  assert.equal(opens('snmp_unreachable'), 'gap')
  assert.equal(opens('udt_new_endpoint'), 'udt')
  assert.equal(opens('netpath_unreachable'), 'netpath')
  assert.equal(opens('service_tls_days'), 'service')
  assert.equal(opens('f5_cert_days'), 'device')
  for (const m of ['appliance_disk_pct', 'vm_alarm', 'agent_no_metrics', 'udt_new_endpoint', 'netpath_rtt', 'service_response_ms']) {
    assert.equal(opens(m), C.ruleSource({ metric: m }), `ruleToState and the catalogue agree on ${m}`)
  }
  // Round trip: a saved virtualisation rule saves back with its source scope and clear level.
  const saved = { name: 'DS', metric: 'vm_datastore_pct', operator: '>', threshold: 85, target: 'conn-1', min_duration: 300, clear_duration: 600,
    conditions: [{ metric: 'vm_datastore_pct', operator: '>', threshold: 85, reset_threshold: 80 }], recovery_alert: true }
  const st = W.ruleToState(saved)
  const body = C.familyPayload(st, W.stateToPayload(st))
  assert.deepEqual([body.metric, body.target, body.min_duration, body.clear_duration, body.trigger_on], ['vm_datastore_pct', 'conn-1', 300, 600, 'any'])
  assert.deepEqual(body.conditions, saved.conditions)
  assert.equal(body.recovery_alert, true)
}

// ── Server depth metrics (migrate-208) ────────────────────────────────────────
{
  const S = await load(`export * from './src/components/forms/serverAlertRule'`)
  const want = { host_swap_pct: '%', host_disk_latency_ms: ' ms', host_nic_errors: ' errors/s', host_clock_skew_s: ' s', host_fs_days_left: ' days' }
  for (const [m, unit] of Object.entries(want)) {
    assert.equal(S.serverMetric(m)?.kind, 'pct', `${m} is a threshold metric`)
    assert.equal(S.serverMetric(m)?.unit, unit)
  }
  assert.equal(S.serverMetricKind('host_auto_service_stopped'), 'auto')
  assert.deepEqual([...S.serverOperators('host_fs_days_left')], ['<', '<='])
  assert.equal(S.serverRuleCondition({ metric: 'host_disk_latency_ms', operator: 'gt', threshold: 50 }), 'Disk latency > 50 ms')
  assert.equal(S.serverRuleCondition({ metric: 'host_nic_errors', operator: '>', threshold: 10 }), 'Network interface errors > 10 errors/s')
  assert.equal(S.serverRuleCondition({ metric: 'host_fs_days_left', operator: 'lt', threshold: 7 }), 'Filesystem full in < 7 days')
  assert.equal(S.serverRuleCondition({ metric: 'host_clock_skew_s', operator: '>', threshold: 60 }), 'Clock offset from the appliance > 60 s')
  assert.equal(S.serverRuleCondition({ metric: 'host_swap_pct', operator: '>', threshold: 80 }), 'Swap usage > 80%')
  assert.equal(S.serverRuleCondition({ metric: 'host_future_metric', operator: '>', threshold: 3 }), 'host_future_metric > 3', 'an unknown metric is not shown as a percentage')
  assert.equal(S.serverRuleCondition({ metric: 'host_auto_service_stopped', operator: 'gt', threshold: 0, target: 'gupdate*' }), 'Automatic service stopped (except gupdate*)')
  assert.equal(S.serverAutoReset('<', 7, 'host_fs_days_left'), 8, 'days until full clears one day above')
  assert.equal(S.serverAutoReset('>', 50, 'host_disk_latency_ms'), null, 'no automatic clear for latency')
  assert.equal(S.serverAutoReset('>', 90, 'host_swap_pct'), 88)
  const draft = (metric, extra = {}) => ({ name: 'x', description: '', enabled: true, severity: 'warning', priority: '', escalation_mode: 'default',
    escalation_policy_id: '', conditions: [{ metric, operator: '>', threshold: 0, reset_threshold: null }], target: '', server_id: '', min_duration: 300,
    clear_duration: null, reset_mode: 'auto', notify_channels: [], cooldown: 300, schedule_start: '', schedule_end: '', schedule_days: [], ...extra })
  let body = S.serverRulePayload(draft('host_auto_service_stopped', { target: ' gupdate*, edgeupdate* ', clear_duration: 600 }))
  assert.deepEqual([body.operator, body.threshold, body.target, body.min_duration, body.clear_duration, body.reset_threshold], ['gt', 0, 'gupdate*, edgeupdate*', 300, 600, null])
  assert.equal(S.serverRulePayload(draft('host_auto_service_stopped')).target, null, 'no services left out')
  assert.match(S.serverDraftProblem(draft('host_fs_days_left', { conditions: [{ metric: 'host_fs_days_left', operator: '>', threshold: 7 }] })).message, /use < or <=/)
  assert.match(S.serverDraftProblem(draft('host_fs_days_left', { conditions: [{ metric: 'host_fs_days_left', operator: '<', threshold: 0 }] })).message, /more than 0/)
  assert.equal(S.serverDraftProblem(draft('host_disk_latency_ms', { conditions: [{ metric: 'host_disk_latency_ms', operator: '>', threshold: 250 }] })), null, 'latency is not capped at 100')
  const switched = S.serverWatchChange({ conditions: [{ metric: 'host_cpu_pct', operator: '>', threshold: 90 }], min_duration: 300, reset_mode: 'auto' }, 'host_fs_days_left')
  assert.deepEqual([switched.conditions[0].operator, switched.conditions[0].threshold], ['<', 7], 'a new metric starts with its own operator and limit')
  assert.equal(S.serverWatchChange({ conditions: [{ metric: 'host_cpu_pct', operator: '>', threshold: 95 }], min_duration: 300, reset_mode: 'auto' }, 'host_swap_pct').conditions[0].threshold, 95, 'percent to percent keeps the limit')
  const d = C.withNewPercentDefaults([{ metric: 'host_swap_pct', operator: '>', threshold: 80, reset_threshold: null }], null)
  assert.equal(d.conditions[0].reset_threshold, 75)
}

console.log('Alert rule catalogue: every family with its inputs, limits and scope; retired keys hidden for new rules; stored operators as symbols; new percent defaults; template state rules in words; list trigger/timers/routing words; recommended rows.')
