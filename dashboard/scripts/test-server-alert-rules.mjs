// Server rules in the alert wizard: a saved host_* rule opens on the Server tab with its own condition, saves through
// /host-alert-rules with that API's fields, and the rule list describes it in words. The seeded "Agent offline" rule
// used to open as a "Ping status > 0" device rule and could not be saved.
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

const wiz = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
const start = wiz.indexOf('const TEMPLATE_FIELDS =')
const end = wiz.indexOf('export function AlertRuleWizardDialog(')
assert(start >= 0 && end > start)
const W = await load(`import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${wiz.slice(start, end)}
export { ruleToState };`)
const S = await load(`export * from './src/components/forms/serverAlertRule'`)

// The seeded Agent offline rule (migrate-168): warning, gt 0, no hold, recovery on.
const seeded = {
  id: 'r1', name: 'Agent offline', metric: 'host_agent_offline', operator: 'gt', threshold: 0, severity: 'warning',
  min_duration: 0, recovery_alert: true, priority: 'P3', notify_channels: [], cooldown: 300, server_id: null,
  trigger_on: 'any', conditions: null, schedule_days: [],
}
let st = W.ruleToState(seeded)
assert.equal(st.source, 'server', 'a host_* rule opens on the Server tab, not as a device rule')
assert.equal(st.conditions.length, 1)
assert.equal(st.conditions[0].metric, 'host_agent_offline')
assert.equal(st.server_id, '')
let pl = S.serverRulePayload(st)
assert.equal(pl.metric, 'host_agent_offline', 'saving keeps the metric')
assert.equal(pl.operator, 'gt'); assert.equal(pl.threshold, 0)
assert.equal(pl.min_duration, 0, '0 keeps the default heartbeat silence')
assert.equal(pl.server_id, null, 'all servers')
assert.equal(pl.recovery_alert, true)
assert.equal(pl.priority, 'P3')
assert.equal(pl.target, null)
for (const k of ['trigger_on', 'device_id', 'group_id', 'conditions', 'email_subject', 'escalation_levels']) {
  assert(!(k in pl), `the server rule body has no ${k}`)
}
assert.equal(S.serverDraftProblem(st), null)

// A percent rule round-trips its operator (stored as words, edited as symbols), reset level, clear hold and scope.
st = W.ruleToState({
  name: 'Disk', metric: 'host_filesystem_pct', operator: 'gte', threshold: 85, severity: 'critical', min_duration: 600,
  clear_duration: 900, server_id: 'srv-9', recovery_alert: true,
  conditions: [{ metric: 'host_filesystem_pct', operator: 'gte', threshold: 85, reset_threshold: 80 }],
  schedule_start: '08:00', schedule_end: '18:00', schedule_days: [1, 2, 3], escalation_policy_id: 'pol-1',
})
assert.equal(st.source, 'server')
assert.equal(st.conditions[0].operator, '>=')
assert.equal(st.conditions[0].reset_threshold, 80)
pl = S.serverRulePayload(st)
assert.deepEqual(
  [pl.operator, pl.threshold, pl.reset_threshold, pl.min_duration, pl.clear_duration, pl.server_id, pl.escalation_policy_id],
  ['>=', 85, 80, 600, 900, 'srv-9', 'pol-1'],
)
assert.deepEqual([pl.schedule_start, pl.schedule_end, pl.schedule_days], ['08:00', '18:00', [1, 2, 3]])

// Service and process rules need a name; a picked-but-empty server scope is caught before saving.
const svc = { ...W.ruleToState({ name: 'SQL', metric: 'host_service_down', threshold: 0, operator: 'gt' }), target: '  ' }
assert.equal(S.serverDraftProblem(svc)?.step, 'trigger')
assert.equal(S.serverRulePayload({ ...svc, target: ' MSSQLSERVER ' }).target, 'MSSQLSERVER')
assert.equal(S.serverRulePayload({ ...svc, target: 'x' }).min_duration, 0, 'service state rules fire on the reported state')
assert.equal(S.serverDraftProblem({ ...svc, target: 'x', server_id: '__pick__' })?.step, 'scope')
assert.equal(S.serverRulePayload({ ...svc, target: 'x', server_id: '__pick__' }).server_id, null)

// The rule list says what the rule watches instead of "Any change / host_agent_offline".
assert.equal(S.serverMetricLabel('host_agent_offline'), 'Agent offline')
assert.equal(S.serverRuleCondition(seeded), 'Agent silent for 3 min, server still answers ping')
assert.equal(S.serverRuleCondition({ ...seeded, min_duration: 60 }), 'Agent silent for 90s, server still answers ping', 'never below the 90 s floor')
assert.equal(S.serverRuleCondition({ metric: 'host_cpu_pct', operator: 'gt', threshold: 90 }), 'CPU usage > 90%')
assert.equal(S.serverRuleCondition({ metric: 'host_process_down', target: 'nginx.exe' }), 'Process nginx.exe not running')
assert.equal(S.serverAutoReset('gt', 90), 88)
assert.equal(S.serverAutoReset('<', 10), 12)

// Lifecycle closing notices are optional; the selected value must survive Save and Duplicate.
for (const metric of ['host_restart', 'host_shutdown']) {
  const life = W.ruleToState({ ...seeded, metric, recovery_alert: false })
  assert.equal(S.serverRulePayload(life).recovery_alert, false)
  assert.equal(S.serverRulePayload({ ...life, reset_mode: 'auto' }).recovery_alert, true)
  assert.equal(S.serverRuleCopy({ ...seeded, metric, recovery_alert: true }).recovery_alert, true)
}
const zero = { ...W.ruleToState({ ...seeded, metric: 'host_cpu_pct', threshold: 0, operator: 'eq', min_duration: 0 }) }
const memory = S.serverWatchChange(zero, 'host_memory_pct')
assert.equal(memory.conditions[0].threshold, 0, 'changing percent metrics preserves a valid zero threshold')
assert.equal(memory.conditions[0].operator, '==', 'changing percent metrics preserves equality')
assert.equal(memory.min_duration, 0, 'changing percent metrics preserves an immediate hold')
const shutdown = S.serverWatchChange(zero, 'host_shutdown')
assert.equal(shutdown.reset_mode, 'none', 'new lifecycle notices default to silent closing')
assert.equal(S.serverWatchChange({ ...zero, ...shutdown, reset_mode: 'auto' }, 'host_restart').reset_mode, 'auto')

// Duplicate copies a server rule through the server API, disabled.
const copy = S.serverRuleCopy({ ...seeded, conditions: null })
assert.equal(copy.name, 'Agent offline (copy)'); assert.equal(copy.enabled, false); assert.equal(copy.metric, 'host_agent_offline')

// Device Down: the same "covers servers" test as server_availability.is_down_rule.
assert.equal(S.downRuleCoversServers({ metric: 'ping_status', trigger_on: 'any', operator: 'eq', threshold: 0 }), true, 'seeded Device Down')
assert.equal(S.downRuleCoversServers({ metric: 'ping_status', trigger_on: 'down' }), true)
assert.equal(S.downRuleCoversServers({ metric: 'ping_status', trigger_on: 'up' }), false)
assert.equal(S.downRuleCoversServers({ metric: 'ping_status', trigger_on: 'any', operator: '>', threshold: 0 }), false, '"ping_status > 0" is up, not down')
assert.equal(S.downRuleCoversServers({ metric: 'ping_status', trigger_on: 'any', conditions: [
  { metric: 'ping_status', operator: '==', threshold: 0 }, { metric: 'rtt', operator: '>', threshold: 100 }] }), false)

// Wording: no third-party product names in the wizard.
assert(!/solarwinds/i.test(wiz), 'the wizard names no other product')
assert(wiz.includes("disabled={isEdit && (src === 'server') !== editingServerRule}"), 'a saved rule cannot switch between server and device families')

const page = fs.readFileSync('src/pages/AlertRulesPage.tsx', 'utf8')
assert(page.includes("api.post('/host-alert-rules', serverRuleCopy(rule))"), 'Duplicate sends a server rule to the server API')

console.log('Server alert rules: wizard opens host_* rules on the Server tab, saves through /host-alert-rules (scope, schedule, reset, policy), validates names and scope, lists them in words; Device Down covers servers as on the server.')
