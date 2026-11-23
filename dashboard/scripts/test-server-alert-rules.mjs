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

const S_DRAFT = () => ({ name: 'x', description: '', enabled: true, severity: 'warning', priority: '', escalation_mode: 'default',
  escalation_policy_id: '', conditions: [{ metric: 'host_cpu_pct', operator: '>', threshold: 90 }], target: '', server_id: '',
  min_duration: 0, clear_duration: null, reset_mode: 'auto', notify_channels: [], cooldown: 300, schedule_start: '',
  schedule_end: '', schedule_days: [] })

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
assert.equal(pl.scope_tag, null, 'all servers: no tag scope')
assert.equal(pl.group_id, null, 'all servers: no group scope')
for (const k of ['trigger_on', 'device_id', 'device_type', 'location', 'conditions', 'email_subject', 'escalation_levels']) {
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

// Tag and group scope (rules without a server): only servers with the tag and/or in the device group.
assert.equal(st.server_scope, 'one', 'a one-server rule opens on "One server"')
const grp = 'aaaaaaaa-0000-0000-0000-000000000001'
let scoped = W.ruleToState({ ...seeded, scope_tag: 'prod', group_id: grp })
assert.equal(scoped.server_scope, 'group', 'a rule with a group (and a tag) opens on "Servers in a group"')
assert.equal(scoped.scope_tag, 'prod'); assert.equal(scoped.group_id, grp)
pl = S.serverRulePayload(scoped)
assert.deepEqual([pl.server_id, pl.scope_tag, pl.group_id], [null, 'prod', grp], 'tag and group are saved together (AND)')
assert.equal(W.ruleToState({ ...seeded, scope_tag: 'prod' }).server_scope, 'tag')
assert.equal(W.ruleToState({ ...seeded, metric: 'ping_status', scope_tag: 'prod' }).server_scope, 'all', 'a device rule tag is not a server scope')
assert.equal(S.serverScopeOf({ server_id: '__pick__', server_scope: 'all' }), 'one', 'a server id always means one server')
assert.equal(S.serverScopeOf({ scope_tag: 'x' }), 'tag')
assert.equal(S.serverScopeOf({ group_id: grp, scope_tag: 'x' }), 'group')
assert.equal(S.serverScopeOf({ server_scope: 'tag' }), 'tag', 'the chosen scope holds while its value is still empty')
// Choosing a scope: one server drops tag and group, tag <-> group keeps both, all servers clears everything.
assert.deepEqual(S.serverScopeChange('one', scoped), { server_scope: 'one', server_id: '__pick__', scope_tag: '', group_id: '' })
assert.deepEqual(S.serverScopeChange('tag', scoped), { server_scope: 'tag', server_id: '', scope_tag: 'prod', group_id: grp })
assert.deepEqual(S.serverScopeChange('all', scoped), { server_scope: 'all', server_id: '', scope_tag: '', group_id: '' })
pl = S.serverRulePayload({ ...scoped, ...S.serverScopeChange('one', scoped), server_id: 'srv-1' })
assert.deepEqual([pl.server_id, pl.scope_tag, pl.group_id], ['srv-1', null, null], 'one server never sends a tag or group (the API refuses that)')
pl = S.serverRulePayload({ ...scoped, ...S.serverScopeChange('all', scoped) })
assert.deepEqual([pl.server_id, pl.scope_tag, pl.group_id], [null, null, null], 'back to all servers clears tag and group')
pl = S.serverRulePayload({ ...scoped, server_scope: 'tag', group_id: '', scope_tag: '  prod  ' })
assert.deepEqual([pl.scope_tag, pl.group_id], ['prod', null], 'the tag is trimmed; no group')
// An incomplete scope is caught before saving.
const tagMode = { ...scoped, ...S.serverScopeChange('all', scoped), server_scope: 'tag' }
assert.equal(S.serverDraftProblem(tagMode)?.step, 'scope', 'tag scope without a tag')
assert.equal(S.serverDraftProblem({ ...tagMode, server_scope: 'group' })?.step, 'scope', 'group scope without a group')
assert.equal(S.serverDraftProblem({ ...tagMode, scope_tag: 'x'.repeat(65) })?.message, 'A tag is at most 64 characters.')
assert.equal(S.serverDraftProblem({ ...tagMode, scope_tag: 'prod' }), null)
assert.equal(S.serverDraftProblem({ ...tagMode, server_scope: 'group', group_id: grp }), null, 'a group alone is enough')
// Rule list wording.
assert.equal(S.serverScopeLabel({}), 'All servers')
assert.equal(S.serverScopeLabel({ server_id: 'srv-1' }, { server: 'sql01' }), 'sql01')
assert.equal(S.serverScopeLabel({ server_id: 'srv-1' }), 'One server')
assert.equal(S.serverScopeLabel({ scope_tag: 'prod' }), 'Servers with tag prod')
assert.equal(S.serverScopeLabel({ group_id: grp }, { group: 'SQL hosts' }), 'Servers in SQL hosts')
assert.equal(S.serverScopeLabel({ group_id: grp, scope_tag: 'prod' }, { group: 'SQL hosts' }), 'Servers in SQL hosts with tag prod')
assert.equal(S.serverScopeLabel({ group_id: grp }), 'Servers in one device group', 'a group the viewer cannot name')
// Duplicate keeps the tag and group scope.
const scopedCopy = S.serverRuleCopy({ ...seeded, scope_tag: 'prod', group_id: grp })
assert.deepEqual([scopedCopy.server_id, scopedCopy.scope_tag, scopedCopy.group_id], [null, 'prod', grp])
assert.match(wiz, /SERVER_SCOPE_OPTIONS\.map/, 'the wizard offers every server scope')
assert.match(wiz, /<TagPicker[\s\S]{0,200}scope_tag/, 'the tag scope reuses the tag picker')
assert.match(wiz, /queryFn: groupsApi\.list/, 'the group scope lists GET /device-groups (static and dynamic)')
assert.match(wiz, /g\.kind === 'dynamic' \? 'dynamic' : 'static'/, 'the group picker shows the group kind')
const tab = fs.readFileSync('src/pages/servers/detail/tabs/AlertsTab.tsx', 'utf8')
assert(tab.includes("r.server_id ? 'This server' : sharedScope(r)"), 'the server Alerts tab names a shared rule\'s scope')

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
// Restart/shutdown alerts close on their own after "Auto-clear after" (clear_duration; null = 15 min).
for (const metric of ['host_restart', 'host_shutdown']) {
  const life = W.ruleToState({ ...seeded, metric, clear_duration: 1800 })
  assert.equal(S.serverRulePayload(life).clear_duration, 1800, `${metric} keeps its auto-clear time`)
  assert.equal(S.serverRulePayload({ ...life, clear_duration: null }).clear_duration, null)
}
assert.equal(S.serverRulePayload(W.ruleToState({ ...seeded, metric: 'host_service_down', target: 'x', clear_duration: 600 })).clear_duration, null)
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

// Combined rules (migrate-211): a saved tree opens as a tree, saves as condition_tree, lists in words.
const tree = { logic: 'OR', items: [
  { logic: 'AND', items: [{ id: 'c1', metric: 'host_cpu_pct', operator: '>', threshold: 90 }, { id: 'c2', metric: 'host_memory_pct', operator: '>', threshold: 90 }] },
  { id: 'c3', metric: 'host_service_down', operator: '>', threshold: 0, target: 'nginx' },
] }
const combined = { ...seeded, id: 'r9', name: 'Server under pressure', metric: 'host_cpu_pct', operator: '>', threshold: 90,
  min_duration: 300, condition_tree: tree }
const cst = W.ruleToState(combined)
assert.equal(cst.source, 'server')
assert.deepEqual(cst.condition_tree, tree, 'a combined rule opens with its tree')
const cbody = S.serverRulePayload({ ...cst, name: 'Server under pressure' })
assert.equal(cbody.condition_tree.logic, 'OR')
assert.equal(cbody.condition_tree.items[1].target, 'nginx')
assert(!('metric' in cbody) && !('reset_threshold' in cbody), 'a combined rule sends its conditions only in the tree')
assert.equal(cbody.min_duration, 300)
assert.equal(S.serverRuleCondition({ metric: 'host_cpu_pct', condition_tree: tree }),
  '(CPU usage > 90% and Memory usage > 90%) or Service nginx stopped')
assert.deepEqual(S.serverRuleCopy({ ...combined, recovery_alert: true }).condition_tree, tree, 'Duplicate keeps the tree')
// A single rule always sends condition_tree: null, so an edit back to one condition turns a combined rule single.
assert.equal(S.serverRulePayload({ ...S_DRAFT(), conditions: [{ metric: 'host_cpu_pct', operator: '>', threshold: 90 }] }).condition_tree, null)
// Add condition: the current condition plus a second one; Use one condition: back to the first.
const grown = S.treeFromDraft({ conditions: [{ metric: 'host_service_down', operator: '>', threshold: 0 }], target: 'sshd' })
assert.equal(grown.items.length, 2)
assert.equal(grown.items[0].target, 'sshd')
assert.deepEqual(S.singleFromTree(grown), { conditions: [{ metric: 'host_service_down', operator: '>', threshold: 0, reset_threshold: null }], target: 'sshd' })
// What stops a save (rule_conditions.validate_tree).
assert.match(S.serverTreeProblem({ logic: 'AND', items: [tree.items[1]] }), /at least two/)
assert.match(S.serverTreeProblem({ logic: 'AND', items: [tree.items[0], { metric: 'host_service_down', operator: '>', threshold: 0, target: ' ' }] }), /service name/)
assert.match(S.serverTreeProblem({ logic: 'AND', items: [tree.items[1], { logic: 'OR', items: [tree.items[1]] }] }), /group holds at least two/)
assert.match(S.serverTreeProblem({ logic: 'AND', items: [tree.items[1], { metric: 'host_cpu_pct', operator: '>', threshold: 120 }] }), /0 to 100/)
assert.match(S.serverTreeProblem({ logic: 'AND', items: [tree.items[1], { metric: 'host_cpu_pct', operator: '>', threshold: 90, reset_threshold: 95 }] }), /healthy side/)
assert.match(S.serverTreeProblem({ logic: 'AND', items: Array(11).fill(tree.items[1]) }), /at most 10/)
assert.equal(S.serverTreeProblem(tree), null)
assert(!S.COMBINABLE_SERVER_METRICS.some((m) => ['host_restart', 'host_shutdown', 'host_agent_offline'].includes(m.value)),
  'events and agent offline stand alone')
assert.equal(S.serverDraftProblem({ ...S_DRAFT(), condition_tree: { logic: 'AND', items: [tree.items[1]] } }).step, 'trigger')

// Wording: no third-party product names in the wizard.
assert(!/solarwinds/i.test(wiz), 'the wizard names no other product')
assert(wiz.includes("disabled={isEdit && (src === 'server') !== editingServerRule}"), 'a saved rule cannot switch between server and device families')

const page = fs.readFileSync('src/pages/AlertRulesPage.tsx', 'utf8')
assert(page.includes("api.post('/host-alert-rules', serverRuleCopy(rule))"), 'Duplicate sends a server rule to the server API')
assert(page.includes('serverScopeLabel(r, {'), 'the Alert Rules list names a server rule\'s tag / group scope')

console.log('Server alert rules: wizard opens host_* rules on the Server tab, saves through /host-alert-rules (scope, schedule, reset, policy), validates names and scope, lists them in words; combined rules open, save and list as condition trees; Device Down covers servers as on the server.')
