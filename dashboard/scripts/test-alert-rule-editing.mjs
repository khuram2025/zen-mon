// Alert rule editing, validation feedback and error display (work/RULES-COVERAGE-PLAN.md "UI"):
//   (a) apiFieldErrors / apiErrorMessage: pydantic 422 arrays with field paths, plain detail strings, never a regex,
//   (b) value domains mirrored from the server (ping_status 0/1, if_oper_status 1–7, percentages 0–100, rates 0–1,
//       counts ≥ 0, never-true / always-true comparisons), ping trigger -> ping_status condition, name/description
//       mismatch warnings, field path -> wizard step,
//   (c) server (host_*) rules: Edit and Duplicate open the server-rule editor; edit sends PUT /host-alert-rules/{id}
//       with that endpoint's update fields only; the editor is prefilled from GET /host-alert-rules,
//   (d) the wizard: ping trigger writes the matching condition, seeded gt/eq operators load as symbols, a failed save
//       keeps the wizard open with a persistent banner listing each field error and opens the owning step.
// Run from dashboard/: node scripts/test-alert-rule-editing.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const root = process.cwd()
const src = path.join(root, 'src')
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
async function load(contents) {
  const out = await build({
    stdin: { contents, resolveDir: root, loader: 'ts' }, bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022',
    jsx: 'automatic', define: { 'process.env.NODE_ENV': '"production"' }, plugins: [aliasPlugin],
    loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' }, logLevel: 'silent',
  })
  return import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)
}

const m = await load(`
export * as U from '@/lib/utils'
export * as C from '@/components/forms/alertRuleChecks'
export * as H from '@/components/alerts/hostRuleModel'
export { HostRuleEditor } from '@/components/alerts/HostRuleDialog'
export { FieldErrorBanner, RuleWarnings } from '@/components/alerts/RuleFeedback'
export { AlertRulesPage } from '@/pages/AlertRulesPage'
export { Dialog } from '@/components/ui/Dialog'
export { api } from '@/lib/api'
export { useAuth } from '@/stores/auth'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
export { QueryClient, QueryClientProvider } from '@tanstack/react-query'
`)
const { U, C, H } = m
const h = m.createElement
const err = (status, data) => ({ response: { status, data } })

/* ═══ (a) field errors ═══════════════════════════════════════════════════════════════════ */
{
  const PATTERN = `String should match pattern '^(ping_status|rtt|packet_loss|jitter|service_status|cpu|memory|uptime_reset|temperature|fan_state|psu_state|if_in_bps|if_out_bps|if_util_pct|if_errors|if_discards|if_oper_status|session_count)$'`
  const e422 = err(422, { detail: [
    { type: 'string_pattern_mismatch', loc: ['body', 'metric'], msg: PATTERN, input: 'host_cpu_pct' },
    { type: 'string_pattern_mismatch', loc: ['body', 'conditions', 0, 'operator'], msg: "String should match pattern '^(>|<|>=|<=|==|!=)$'", input: 'gt' },
    { type: 'value_error', loc: ['body', 'conditions', 1, 'threshold'], msg: "Value error, ping_status is 0 (down) or 1 (up); '> 5' can never be true" },
  ] })
  const fe = U.apiFieldErrors(e422)
  assert.deepEqual(fe.map((x) => x.path), ['metric', 'conditions[0].operator', 'conditions[1].threshold'], 'loc becomes a field path')
  assert.equal(fe[0].msg, "'host_cpu_pct' is not an accepted value", 'a pattern error names the value, not the regex')
  assert.equal(fe[2].msg, "ping_status is 0 (down) or 1 (up); '> 5' can never be true", 'the pydantic "Value error," prefix is dropped')
  const text = U.apiErrorMessage(e422)
  assert(!text.includes('^(') && !text.includes('should match pattern'), 'the message never dumps a regex')
  assert(text.startsWith("metric: 'host_cpu_pct' is not an accepted value; conditions[0].operator: 'gt' is not an accepted value"), text)
  assert(text.length < 250)
  // A server that already renders "<field>: …" is not prefixed twice; contract message for host metrics.
  const named = err(422, { detail: [{ loc: ['body', 'metric'], msg: "metric: 'host_cpu_pct' is not an accepted value" }, { loc: ['body', 'metric'], msg: 'Server rules are edited under Servers → Alerts (host-alert-rules)' }] })
  assert.deepEqual(U.apiFieldErrors(named), [{ path: 'metric', msg: "'host_cpu_pct' is not an accepted value" }, { path: 'metric', msg: 'Server rules are edited under Servers → Alerts (host-alert-rules)' }])
  assert.deepEqual(U.apiFieldErrors(err(400, { detail: 'target is required for host_service_down' })), [{ path: '', msg: 'target is required for host_service_down' }])
  assert.equal(U.apiErrorMessage(err(400, { detail: 'Escalation policy not found' })), 'Escalation policy not found', 'plain strings unchanged')
  assert.deepEqual(U.apiFieldErrors(err(422, { detail: { loc: ['body', 'target'], msg: 'Service name is required' } })), [{ path: 'target', msg: 'Service name is required' }])
  assert.equal(U.apiErrorMessage(err(400, { detail: { message: 'Bad window' } })), 'Bad window')
  assert.equal(U.apiErrorMessage(new Error('Network Error')), 'Network Error')
  assert.equal(U.apiErrorMessage({}, 'fallback'), 'fallback')
  assert.equal(U.apiErrorMessage(err(422, { detail: [{ loc: ['body'], msg: 'x'.repeat(1000) }] })).length <= 300, true, 'a long message is cut')
}

/* ═══ (b) value domains, ping trigger, warnings, step mapping ════════════════════════════ */
{
  const D = C.conditionDomainError
  assert.equal(D('ping_status', '>', 5), "ping_status is 0 (down) or 1 (up); '> 5' can never be true")
  assert.match(D('ping_status', '<', 0), /can never be true/)
  assert.match(D('ping_status', '==', 7), /can never be true/)
  assert.match(D('ping_status', '>=', 0), /is always true/)
  assert.equal(D('ping_status', '==', 0), null)
  assert.equal(D('ping_status', '!=', 1), null)
  assert.match(D('if_oper_status', '>', 7), /1 \(up\) to 7.*can never be true/)
  assert.match(D('if_oper_status', '>', 0), /is always true/)
  assert.equal(D('if_oper_status', '==', 2), null)
  assert.match(D('uptime_reset', '==', 2), /can never be true/)
  assert.equal(D('uptime_reset', 'eq', 1), null, 'host/seeded operator words are understood')
  assert.match(D('cpu', '>', 150), /cpu is a percentage from 0 to 100; '> 150' can never be true/)
  assert.match(D('host_cpu_pct', 'gt', 100), /can never be true/)
  assert.equal(D('host_cpu_pct', 'gte', 100), null)
  assert.match(D('memory', '<=', 100), /always true/)
  assert.match(D('packet_loss', '<', 0), /never be true/)
  assert.equal(D('if_util_pct', '>', 80), null)
  assert.match(D('apm_error_rate', '>', 2), /fraction from 0 to 1/)
  assert.equal(D('apm_error_rate', '>', 0.05), null)
  assert.match(D('apm_apdex', '<', 0), /never be true/)
  assert.match(D('rtt', '<', 0), /never negative.*can never be true/)
  assert.match(D('if_errors', '>=', 0), /always true/)
  assert.equal(D('if_errors', '>', 0), null)
  assert.equal(D('temperature', '>', -10), null, 'temperature has no fixed range')
  assert.equal(D('service_status', '>', 99), null, 'operator/threshold ignored for status events')
  assert.equal(D('udt_new_endpoint', '==', 1), null)
  assert.equal(D('cpu', '>', NaN), 'Enter a number for the value')
  assert.equal(D('cpu', '>', ''), 'Enter a number for the value')
  assert.equal(C.resetDomainError('cpu', 120), 'cpu is a percentage from 0 to 100; a reset level of 120 can never be reached')
  assert.equal(C.resetDomainError('cpu', null), null)
  assert.equal(C.resetDomainError('rtt', 50), null)

  assert.deepEqual(C.pingConditionFor('down'), { operator: '==', threshold: 0 })
  assert.deepEqual(C.pingConditionFor('up'), { operator: '==', threshold: 1 })
  assert.deepEqual(C.pingConditionFor('degraded'), { operator: '==', threshold: 1 })
  assert.equal(C.pingConditionFor('any'), null)
  assert.deepEqual(C.syncPingConditions([{ metric: 'ping_status', operator: '==', threshold: 0 }, { metric: 'rtt', operator: '>', threshold: 100 }], 'up'),
    [{ metric: 'ping_status', operator: '==', threshold: 1 }, { metric: 'rtt', operator: '>', threshold: 100 }])
  const any = [{ metric: 'ping_status', operator: '!=', threshold: 1 }]
  assert.equal(C.syncPingConditions(any, 'any'), any, '"any change" keeps the condition as written')
  assert.equal(C.normalizeOperator('gte'), '>=')
  assert.equal(C.normalizeOperator('<'), '<')

  const W = (name, conditions, trigger_on, description) => C.ruleNameWarnings({ name, description, conditions, trigger_on })
  const cpu90 = [{ metric: 'cpu', operator: '>', threshold: 90 }]
  assert.deepEqual(W('High CPU usage', cpu90), [], 'a consistent rule has no warning')
  assert.match(W('High memory', cpu90)[0].message, /The name mentions memory, but the condition checks device CPU/)
  assert.match(W('Core router CPU high', [{ metric: 'ping_status', operator: '==', threshold: 0 }], 'down')[0].message, /mentions CPU, but the condition checks ping status/)
  assert.match(W('High CPU', [{ metric: 'cpu', operator: '<', threshold: 10 }])[0].message, /says high, but the condition fires when device CPU is below 10/)
  assert.match(W('Low throughput', [{ metric: 'apm_throughput', operator: '>', threshold: 100 }])[0].message, /says low/)
  assert.deepEqual(W('Low disk space', [{ metric: 'host_filesystem_pct', operator: 'gt', threshold: 90 }]), [], '"low disk space" means high usage')
  assert.match(W('CPU above 90%', [{ metric: 'cpu', operator: '>', threshold: 85 }])[0].message, /says 90%, but the threshold is 85%/)
  assert.deepEqual(W('CPU above 90%', cpu90), [])
  assert.match(W('Device recovered', [{ metric: 'ping_status', operator: '==', threshold: 0 }], 'down')[0].message, /recovers, but the rule triggers when it goes down/)
  assert.match(W('Device down', [{ metric: 'ping_status', operator: '==', threshold: 1 }], 'up')[0].message, /says down, but the rule triggers when it comes up/)
  assert.deepEqual(W('Device down', [{ metric: 'ping_status', operator: '==', threshold: 0 }], 'down'), [])
  assert.deepEqual(W('Interface down', [{ metric: 'if_oper_status', operator: '==', threshold: 2 }]), [])
  assert.match(W('Interface down', [{ metric: 'ping_status', operator: '==', threshold: 0 }], 'down')[0].message, /mentions interface/)
  assert.deepEqual(W('Port 443 listener', [{ metric: 'service_status', operator: '==', threshold: 0 }], 'down'), [], 'service names are not checked against metric families')
  assert.equal(W('CPU high', cpu90, null, 'Fires when memory is above 90%')[0].field, 'description', 'the description is checked too')
  assert.deepEqual(C.warningsFromApi({ warnings: [{ field: 'name', message: 'Name says CPU' }, 'plain', { msg: '' }] }), [{ field: 'name', message: 'Name says CPU' }, { field: '', message: 'plain' }])
  assert.deepEqual(C.warningsFromApi({ id: 'x' }), [], 'an older server sends none')
  assert.equal(C.mergeWarnings([{ field: 'name', message: 'A' }], [{ field: '', message: 'a ' }]).length, 1)

  const S = C.wizardStepForPath
  assert.equal(S('metric'), 'trigger')
  assert.equal(S('conditions[0].operator'), 'trigger')
  assert.equal(S('threshold'), 'trigger')
  assert.equal(S('name'), 'properties')
  assert.equal(S('device_id'), 'scope')
  assert.equal(S('target'), 'trigger')
  assert.equal(S('target', 'apm'), 'scope')
  assert.equal(S('escalation_levels[1].after_minutes'), 'escalation')
  assert.equal(S('escalation_policy_id'), 'escalation')
  assert.equal(S('recovery_sms_template'), 'reset_actions')
  assert.equal(S('notify_channels'), 'actions')
  assert.equal(S('schedule_days'), 'schedule')
  assert.equal(S(''), 'review')
  assert.equal(C.fieldLabel('conditions[0].threshold'), 'Condition 1 value')
}

/* ═══ (c) server rules: model, requests, editor, routing ═════════════════════════════════ */
const SERVER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const HOST_ROW = {
  id: 'h1', name: 'Server CPU high', description: 'CPU above 90% for 5 minutes', enabled: true, metric: 'host_cpu_pct', operator: 'gt', threshold: 90,
  severity: 'critical', min_duration: 300, clear_duration: 600, reset_threshold: 80, auto_reset_threshold: null, recovery_alert: true,
  cooldown: 300, notify_channels: ['ch-1'], server_id: SERVER, target: null, priority: 'P2', escalation_policy_id: 'pol-1',
}
{
  assert(H.isHostRule({ metric: 'host_cpu_pct' }) && H.isHostRule({ metric: 'host_service_down' }))
  assert(!H.isHostRule({ metric: 'cpu' }) && !H.isHostRule(null))

  const f = H.hostRuleFormFromRule(HOST_ROW, 'edit')
  assert.deepEqual([f.name, f.metric, f.operator, f.threshold, f.minutes, f.clearMinutes, f.resetLevel, f.recovery, f.priority, f.policyId, f.scopeServerId, f.allServers],
    ['Server CPU high', 'host_cpu_pct', 'gt', '90', '5', '10', '80', true, 'P2', 'pol-1', SERVER, false], 'prefilled from GET /host-alert-rules')
  const put = H.hostRuleRequest('edit', { ...f, threshold: '95' }, 'h1')
  assert.equal(put.method, 'put')
  assert.equal(put.url, '/host-alert-rules/h1', 'edit saves to the host endpoint, never /alert-rules')
  assert.deepEqual(Object.keys(put.body).filter((k) => !H.HOST_RULE_UPDATE_FIELDS.includes(k)), [], 'only HostRuleUpdate fields')
  assert(!('server_id' in put.body), 'the scope is not an update field')
  assert.deepEqual([put.body.threshold, put.body.min_duration, put.body.clear_duration, put.body.reset_threshold, put.body.priority, put.body.escalation_policy_id, put.body.target],
    [95, 300, 600, 80, 'P2', 'pol-1', null])
  assert.equal(H.hostRuleRequest('edit', { ...f, resetLevel: '', clearMinutes: '', priority: 'auto', policyId: 'default' }, 'h1').body.reset_threshold, null, 'an emptied clear level returns to automatic')
  assert.throws(() => H.hostRuleRequest('edit', f, null))

  const svc = H.hostRuleFormFromRule({ ...HOST_ROW, id: 'h2', metric: 'host_service_down', operator: 'eq', threshold: 0, target: 'MSSQLSERVER', server_id: null }, 'edit')
  const svcPut = H.hostRuleRequest('edit', svc, 'h2').body
  assert.equal(svcPut.target, 'MSSQLSERVER', 'the service name is kept on edit (never erased)')
  // Editing a service/process rule keeps its hold (min_duration 300 here), never resets it to 0.
  assert.deepEqual([svcPut.threshold, svcPut.min_duration, svcPut.reset_threshold], [0, 300, null])
  const oddHold = H.hostRuleRequest('edit', H.hostRuleFormFromRule({ ...HOST_ROW, id: 'h3', metric: 'host_process_down', operator: 'eq', threshold: 0, target: 'nginx', min_duration: 45 }, 'edit'), 'h3').body
  assert.equal(oddHold.min_duration, 45, 'a process rule hold round-trips exactly')
  assert.equal(H.hostRuleRequest('duplicate', H.hostRuleFormFromRule({ ...HOST_ROW, metric: 'host_service_down', operator: 'eq', threshold: 0, target: 'x' }, 'duplicate')).body.min_duration, 300, 'a copy keeps the hold')
  assert.equal(H.hostRuleRequest('create', H.emptyHostRuleForm(SERVER, { metric: 'host_service_down', target: 'nginx' })).body.min_duration, 0, 'a new service rule starts at 0')
  assert.deepEqual(H.validateHostRuleForm({ ...svc, target: ' ' }), [{ path: 'target', msg: 'Service name is required' }])

  const dup = H.hostRuleFormFromRule(HOST_ROW, 'duplicate')
  assert.equal(dup.name, 'Server CPU high (copy)')
  assert.equal(dup.enabled, false, 'a copy starts disabled')
  const post = H.hostRuleRequest('duplicate', dup)
  assert.deepEqual([post.method, post.url, post.body.server_id, post.body.operator], ['post', '/host-alert-rules', SERVER, 'gt'], 'duplicate creates on the host endpoint with the same scope')
  assert.equal(H.hostRuleRequest('duplicate', H.hostRuleFormFromRule({ ...HOST_ROW, server_id: null }, 'duplicate')).body.server_id, null, 'a global rule copies as global')
  const create = H.hostRuleRequest('create', H.emptyHostRuleForm(SERVER, { metric: 'host_service_down', target: 'nginx', name: 'nginx stopped' }))
  assert.deepEqual([create.body.server_id, create.body.target, create.body.threshold, create.body.name], [SERVER, 'nginx', 0, 'nginx stopped'])

  assert.deepEqual(H.validateHostRuleForm({ ...f, threshold: '150' }).map((e) => e.path), ['threshold'], 'percent thresholds are 0–100')
  assert.match(H.validateHostRuleForm({ ...f, threshold: '150' })[0].msg, /host_cpu_pct is a percentage from 0 to 100; '> 150' can never be true/)
  assert.deepEqual(H.validateHostRuleForm({ ...f, resetLevel: '95' }).map((e) => e.path), ['reset_threshold'], 'the clear level must be on the healthy side')
  assert.deepEqual(H.validateHostRuleForm({ ...f, resetLevel: '120' }).map((e) => e.path), ['reset_threshold'])
  assert.deepEqual(H.validateHostRuleForm(f), [])
  assert.equal(H.ruleCondition(HOST_ROW), 'CPU usage > 90%')
  assert.equal(H.ruleClearing(HOST_ROW), 'clears below 80% for 10 min')
  assert.equal(H.autoReset('>', 90), 88)
}

// Rendering: seeded react-query data, every request recorded.
const calls = []
for (const verb of ['get', 'post', 'put', 'delete', 'patch']) m.api[verb] = (...args) => { calls.push([verb, args[0]]); return new Promise(() => {}) }
m.useAuth.setState({ user: { id: 'me', username: 'me', email: 'me@example.com', role: 'custom', permissions: ['alerts.view', 'alerts.manage'] }, token: 't' })
const newClient = () => new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false, staleTime: Infinity } } })
const strip = (html) => html.replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()
const render = (qc, el) => m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.Dialog, { open: true }, el))))
{
  // Edit from the Alert Manager: the generic row lacks server_id and the reset level; the editor completes it from
  // GET /host-alert-rules.
  const qc = newClient()
  qc.setQueryData(['host-alert-rules', 'all'], { items: [HOST_ROW] })
  qc.setQueryData(['escalation', 'policies'], [])
  qc.setQueryData(['notification-channels'], { data: [{ id: 'ch-1', name: 'NOC', type: 'email' }] })
  calls.length = 0
  const generic = { id: 'h1', name: 'Server CPU high', metric: 'host_cpu_pct', operator: 'gt', threshold: 90, severity: 'critical', enabled: true }
  const html = render(qc, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, rule: generic, mode: 'edit' }))
  const text = strip(html)
  assert(text.includes('Edit server alert rule') && text.includes('Server rules are saved under Servers → Alerts'))
  assert(html.includes('value="Server CPU high"') && html.includes('value="90"') && html.includes('value="80"') && html.includes('value="10"'), 'prefilled: name, threshold, reset level, clear hold')
  assert(text.includes('CPU above 90% for 5 minutes'), 'description')
  assert(text.includes('Save rule') && !text.includes('Create rule'))
  assert(!text.includes('Apply to all servers'), 'the scope is fixed on edit')
  assert(html.includes('aria-pressed="true"'), 'its channel is selected')
  assert.deepEqual(calls.filter(([verb]) => verb !== 'get'), [], 'rendering saves nothing')

  // GET /host-alert-rules failed: no fallback to the generic row (it lacks server_id and the reset level), the load
  // error is shown and Save is disabled; same when the rule is missing from the list.
  for (const [mode, label] of [['edit', 'Save rule'], ['duplicate', 'Create rule']]) {
    const failed = newClient()
    failed.setQueryData(['escalation', 'policies'], [])
    failed.getQueryCache().build(failed, { queryKey: ['host-alert-rules', 'all'] }).setState({
      status: 'error', fetchStatus: 'idle', error: Object.assign(new Error('Request failed'), { response: { status: 503, data: { detail: 'Service unavailable' } } }),
      errorUpdatedAt: Date.now(), data: undefined, dataUpdatedAt: 0,
    })
    const fh = render(failed, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, rule: generic, mode }))
    const ft = strip(fh)
    assert(ft.includes('Could not load this server rule from Servers → Alerts: Service unavailable'), `${mode}: the load error is shown`)
    assert(!fh.includes('value="Server CPU high"') && !fh.includes('id="host-rule-threshold"'), `${mode}: no form prefilled from the generic row`)
    assert(new RegExp(`<button[^>]*disabled=""[^>]*>${label}</button>`).test(fh), `${mode}: Save is disabled`)
  }
  const missing = newClient()
  missing.setQueryData(['host-alert-rules', 'all'], { items: [] })
  missing.setQueryData(['escalation', 'policies'], [])
  const mh = render(missing, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, rule: generic, mode: 'edit' }))
  assert(strip(mh).includes('This server rule was not found under Servers → Alerts') && /<button[^>]*disabled=""[^>]*>Save rule<\/button>/.test(mh), 'a rule missing from the list cannot be saved')
  const dlgSrc = fs.readFileSync('src/components/alerts/HostRuleDialog.tsx', 'utf8')
  assert(!dlgSrc.includes('listQ.isError ? rule as HostRule'), 'no fallback to the generic row')
  assert(dlgSrc.includes('if (loadError || loading) return'), 'submit refuses without the full rule')

  // A value outside the metric's range shows inline at once; a mismatching name shows an amber warning.
  const bad = render(qc, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, rule: { ...HOST_ROW, name: 'High memory', threshold: 150 }, mode: 'edit' }))
  const bt = strip(bad)
  assert(bt.includes("host_cpu_pct is a percentage from 0 to 100; '> 150' can never be true"), 'inline value-domain message')
  assert(bt.includes('Check the name against the condition') && bt.includes('The name mentions memory, but the condition checks CPU usage'), 'amber mismatch warning')
  assert(bt.includes('Saving is still allowed'))

  // Duplicate and create.
  const d = strip(render(qc, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, rule: HOST_ROW, mode: 'duplicate' })))
  assert(d.includes('Duplicate server alert rule') && d.includes('Create rule') && d.includes('created disabled'))
  const c = strip(render(qc, h(m.HostRuleEditor, { open: true, onOpenChange: () => {}, serverId: SERVER, initial: { metric: 'host_service_down', target: 'nginx' } })))
  assert(c.includes('New alert rule') && c.includes('Service name') && c.includes('Apply to all servers'))
}
{
  // The Alert Manager routes host_* rules (Edit and Duplicate) to the server-rule editor.
  const page = fs.readFileSync('src/pages/AlertRulesPage.tsx', 'utf8')
  assert(/const openEdit = \(r: any\) => \{\s*if \(isHostRule\(r\)\) setHostEditing\(\{ rule: r, mode: 'edit' \}\)/.test(page), 'Edit of a host rule opens the server-rule editor')
  assert(/const openDuplicate = \(r: any\) => \{\s*if \(isHostRule\(r\)\) setHostEditing\(\{ rule: r, mode: 'duplicate' \}\)/.test(page), 'Duplicate of a host rule too')
  assert(page.includes('onClick={() => openEdit(r)}') && page.includes('onClick={() => openDuplicate(r)}'))
  assert(/<HostRuleDialog[\s\S]*?rule=\{hostEditing\?\.rule \?\? null\}/.test(page))
  assert(page.includes("api.post('/alert-rules', duplicateRulePayload(rule))"), 'duplicate posts duplicateRulePayload')
  // Duplicating a seeded gt/eq rule sends symbols; an old-wizard "up" row (ping_status == 0) is copied as == 1.
  const seeded = C.duplicateRulePayload({ id: 'r1', created_at: 'x', updated_at: 'y', created_by: 'u', name: 'Interface down', metric: 'if_oper_status', operator: 'eq', threshold: 2, trigger_on: 'any', conditions: null })
  assert.equal(seeded.operator, '==')
  assert.equal(seeded.name, 'Interface down (copy)')
  assert.equal(seeded.enabled, false)
  assert(!('id' in seeded) && !('created_by' in seeded))
  const oldUp = C.duplicateRulePayload({ id: 'r2', name: 'Device back up', metric: 'ping_status', operator: 'eq', threshold: 0, trigger_on: 'up', conditions: null })
  assert.deepEqual([oldUp.operator, oldUp.threshold], ['==', 1], 'flat ping_status follows trigger up')
  const oldDown = C.duplicateRulePayload({ id: 'r3', name: 'Down and slow', metric: 'ping_status', operator: '==', threshold: 1, trigger_on: 'down',
    conditions: [{ metric: 'ping_status', operator: 'eq', threshold: 1 }, { metric: 'rtt', operator: 'gt', threshold: 200 }] })
  assert.deepEqual(oldDown.conditions, [{ metric: 'ping_status', operator: '==', threshold: 0 }, { metric: 'rtt', operator: '>', threshold: 200 }])
  assert.deepEqual([oldDown.operator, oldDown.threshold], ['==', 0], 'flat mirror follows conditions[0]')
  const anyRule = C.duplicateRulePayload({ name: 'Ping any', metric: 'ping_status', operator: '==', threshold: 0, trigger_on: 'any' })
  assert.equal(anyRule.threshold, 0, "trigger 'any' keeps the condition")
  const qc = newClient()
  qc.setQueryData(['escalation', 'policies'], [])
  qc.setQueryData(['alert-rules'], [{ id: 'h1', name: 'Server CPU high', metric: 'host_cpu_pct', operator: 'gt', threshold: 90, severity: 'critical', enabled: true, created_at: '2026-10-01T00:00:00Z' }])
  const text = strip(m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.AlertRulesPage)))))
  assert(text.includes('Server CPU high') && text.includes('Servers'), 'host rules are labelled with a server scope')
  const html = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, h(m.AlertRulesPage))))
  assert(html.includes('title="Edit server rule"'))

  // The alert detail's policy card: a server rule's Edit opens the server-rule editor (PUT /host-alert-rules),
  // never the generic quick editor that PUTs /alert-rules.
  const card = fs.readFileSync('src/components/alerts/PolicyCard.tsx', 'utf8')
  assert(card.includes('const hostRule = isHostRule(rule)'))
  assert(/\{canEdit && editOpen && hostRule && \(\s*<HostRuleDialog[\s\S]*?rule=\{rule\}\s*mode="edit"/.test(card), 'PolicyCard opens HostRuleDialog in edit mode for host_* rules')
  assert(card.includes('{canEdit && !compound && editOpen && !hostRule && ('), 'the quick editor is for device rules only')

  // The server tab: every rule row has Edit; the dialog saves through hostRuleRequest (PUT for edit).
  const tab = fs.readFileSync('src/pages/servers/detail/tabs/AlertsTab.tsx', 'utf8')
  assert(tab.includes('aria-label={`Edit ${r.name}`}') && tab.includes('onClick={() => setEditing(r)}'), 'AlertsTab rows get Edit')
  assert(/<HostRuleDialog[\s\S]*?rule=\{editing\}[\s\S]*?mode=\{editing \? 'edit' : 'create'\}/.test(tab))
  assert(!tab.includes('function CreateHostRuleDialog'), 'one editor for create and edit')
  const dlg = fs.readFileSync('src/components/alerts/HostRuleDialog.tsx', 'utf8')
  assert(dlg.includes("const req = hostRuleRequest(mode, form, source?.id ?? rule?.id)") && dlg.includes("req.method === 'put' ? await api.put(req.url, req.body)"))
  assert(!dlg.includes("'/alert-rules/"), 'the server-rule editor never writes /alert-rules')
}

/* ═══ (d) the inline banner and the wizard ═══════════════════════════════════════════════ */
{
  const errors = U.apiFieldErrors(err(422, { detail: [
    { loc: ['body', 'conditions', 0, 'threshold'], msg: "ping_status is 0 (down) or 1 (up); '> 5' can never be true" },
    { loc: ['body', 'name'], msg: 'Field required' },
  ] }))
  const banner = strip(m.renderToStaticMarkup(h(m.FieldErrorBanner, { errors, onJump: () => {} })))
  assert(banner.includes('The rule was not saved'))
  assert(banner.includes("conditions[0].threshold → ping_status is 0 (down) or 1 (up); '> 5' can never be true") && banner.includes('name → Field required'), 'each field error: path -> message')
  assert(banner.includes('this message clears when you edit'))
  assert.equal(m.renderToStaticMarkup(h(m.FieldErrorBanner, { errors: [] })), '', 'nothing without errors')
  assert.equal(C.wizardStepForPath(errors[0].path), 'trigger', 'the first error opens the trigger step')

  // The server-rule dialog: a failed save keeps it open with the banner, cleared by the next edit.
  const dlg = fs.readFileSync('src/components/alerts/HostRuleDialog.tsx', 'utf8')
  assert(dlg.includes('onError: (e, form) => setFailure({ errors: apiFieldErrors(e), form })'), '422 -> banner')
  assert(dlg.includes('const banner = failure && failure.form === f ? failure.errors : []'), 'the banner clears when the form changes')
  const onError = dlg.slice(dlg.indexOf('onError: (e, form)'), dlg.indexOf('const submit'))
  assert(!onError.includes('onOpenChange(false)'), 'the dialog stays open on an error')
  assert(dlg.includes('<FieldErrorBanner errors={banner} />'))

  // The wizard: same behaviour, and it jumps to the step that owns the field.
  const wiz = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
  assert(wiz.includes('onError: (e: any, vars) => fail(apiFieldErrors(e), undefined, vars.state)'), 'the 422 field errors reach the banner')
  assert(wiz.includes('const bannerErrors = saveFailure && saveFailure.state === s ? saveFailure.errors : []'), 'cleared by the next edit')
  assert(/function fail\([\s\S]*?setSaveFailure\(\{ errors: list, state \}\)[\s\S]*?wizardStepForPath\(/.test(wiz), 'fail() lists the errors and opens the owning step')
  const saveBlock = wiz.slice(wiz.indexOf('const save = useMutation({'), wiz.indexOf('function fail('))
  assert.equal((saveBlock.match(/onOpenChange\(false\)/g) ?? []).length, 1, 'only a successful save closes the wizard')
  assert(saveBlock.indexOf('onOpenChange(false)') < saveBlock.indexOf('onError'))
  assert(!wiz.includes("toast.error('Save failed'"), 'no vanishing toast for a failed save')
  assert(wiz.includes('<FieldErrorBanner errors={bannerErrors} onJump={(path) => setStep(wizardStepForPath(path, s.source))} />'))
  assert(wiz.includes('conditionDomainError(c.metric, c.operator, c.threshold)') && wiz.includes("fail(domain, 'trigger')"), 'value domains are checked before saving')
  assert(wiz.includes('<RuleWarnings warnings={nameWarnings} />'), 'Review shows the amber warnings')

  // Round trip through the wizard's own ruleToState / stateToPayload (sliced like test-network-contracts.mjs).
  const start = wiz.indexOf('const TEMPLATE_FIELDS =')
  const end = wiz.indexOf('export function AlertRuleWizardDialog(')
  const Wz = await load(`import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
import { normalizeOperator, syncPingConditions } from './src/components/forms/alertRuleChecks';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${wiz.slice(start, end)}
export { ruleToState, stateToPayload };`)
  const up = Wz.stateToPayload({ ...Wz.ruleToState({ name: 'Device comes up', metric: 'ping_status', operator: '==', threshold: 0, trigger_on: 'down' }), trigger_on: 'up' })
  assert.deepEqual([up.metric, up.operator, up.threshold, up.trigger_on], ['ping_status', '==', 1, 'up'], 'trigger up writes ping_status == 1')
  const deg = Wz.stateToPayload({ ...Wz.ruleToState({ name: 'Degraded', metric: 'ping_status', trigger_on: 'down' }), trigger_on: 'degraded' })
  assert.deepEqual([deg.operator, deg.threshold], ['==', 1])
  const down = Wz.stateToPayload(Wz.ruleToState({ name: 'Down', metric: 'ping_status', operator: '==', threshold: 1, trigger_on: 'down' }))
  assert.deepEqual([down.operator, down.threshold], ['==', 0], 'a stored contradiction is corrected on load and save')
  const multi = Wz.stateToPayload(Wz.ruleToState({ name: 'Down + slow', metric: 'ping_status', trigger_on: 'up', conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }, { metric: 'rtt', operator: '>', threshold: 200 }] }))
  assert.deepEqual(multi.conditions, [{ metric: 'ping_status', operator: '==', threshold: 1, reset_threshold: null }, { metric: 'rtt', operator: '>', threshold: 200, reset_threshold: null }])
  const anyRule = Wz.stateToPayload(Wz.ruleToState({ name: 'Any', metric: 'ping_status', operator: '!=', threshold: 1, trigger_on: 'any' }))
  assert.deepEqual([anyRule.operator, anyRule.threshold, anyRule.trigger_on], ['!=', 1, 'any'], '"any change" keeps the written condition')
  const seeded = Wz.ruleToState({ name: 'Reboot', metric: 'uptime_reset', operator: 'eq', threshold: 1 })
  assert.equal(seeded.conditions[0].operator, '==', 'seeded eq loads as ==')
  assert.equal(Wz.stateToPayload(seeded).operator, '==')
  assert.equal(Wz.ruleToState({ name: 'C', metric: 'cpu', conditions: [{ metric: 'cpu', operator: 'gt', threshold: 90, reset_threshold: 80 }] }).conditions[0].operator, '>')
}

console.log('Alert rule editing: field errors with paths (no regex), value domains and never/always-true comparisons, ping trigger conditions, name/condition warnings, step mapping, server-rule editor (PUT /host-alert-rules/{id}, prefilled, duplicate/create), Alert Manager routing, persistent error banner keeping dialogs open.')
