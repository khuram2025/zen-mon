// Thresholds per class (Device Templates phase 3, migrate-193, docs/alert-rule-overrides.md): which rules take
// overrides, how a condition, a recommendation change and an override read, interface role rules the server would
// refuse, and that each UI entry point is wired (template Recommended alerts tab and roles editor, rule overrides
// dialog, device thresholds card and interface roles, wizard template metric / scope / role).
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
const out = await build({
  stdin: { contents: `export * from './src/components/alerts/thresholdModel'`, resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', plugins: [aliasPlugin], logLevel: 'silent',
})
const M = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)

// Which rules take overrides (alert_rules._overridable).
assert.equal(M.ruleTakesOverrides({ metric: 'cpu' }), true)
assert.equal(M.ruleTakesOverrides({ metric: 'tpl_fgt_cpu' }), true)
assert.equal(M.ruleTakesOverrides({ metric: 'if_util_pct' }), true)
assert.equal(M.ruleTakesOverrides({ metric: 'host_cpu_pct' }), true)
assert.equal(M.ruleTakesOverrides({ metric: 'host_filesystem_pct' }), true)
for (const metric of ['ping_status', 'trap', 'syslog', 'service_status', 'uptime_reset', 'host_service_down', 'host_agent_offline', 'apm_latency_p95', 'nf_bytes']) {
  assert.equal(M.ruleTakesOverrides({ metric }), false, `${metric} takes no override`)
}
assert.equal(M.ruleTakesOverrides({ metric: 'cpu', conditions: [{ metric: 'cpu' }, { metric: 'ping_status' }] }), false, 'a status condition makes the rule non-overridable')
assert.equal(M.overrideAssetKind({ metric: 'host_cpu_pct' }), 'server')
assert.equal(M.overrideAssetKind({ metric: 'tpl_x' }), 'device')
assert.equal(M.ruleHasInstances({ metric: 'if_util_pct' }), true)
assert.equal(M.ruleHasInstances({ metric: 'cpu' }), false)

// Words.
assert.equal(M.ruleWords({ conditions: [{ metric: 'cpu', operator: '>=', threshold: 90 }], min_duration: 300 }), 'CPU ≥ 90% for 5 min')
assert.equal(M.conditionWords({ metric: 'tpl_fgt_cpu', operator: '>=', threshold: 95 }, { names: { fgt_cpu: 'CPU Usage' }, unit: '%' }), 'CPU Usage ≥ 95%')
assert.equal(M.conditionWords({ metric: 'if_oper_status', operator: '==', threshold: 2 }), 'Interface down')
// Interface quality (alerting plan G-12): rates, a flap window, a speed-change event.
assert.equal(M.ruleWords({ conditions: [{ metric: 'if_error_rate_pct', operator: '>', threshold: 1 }], min_duration: 600 }),
  'Interface error rate > 1% of packets for 10 min')
assert.equal(M.ruleWords({ conditions: [{ metric: 'if_flaps', operator: '>=', threshold: 5 }], min_duration: 900 }),
  'Interface state changes ≥ 5 in 15 min')
assert.equal(M.ruleWords({ conditions: [{ metric: 'if_flaps', operator: '>=', threshold: 5 }], min_duration: null }),
  'Interface state changes ≥ 5 in 15 min')
assert.equal(M.ruleWords({ conditions: [{ metric: 'if_speed_changed', operator: '==', threshold: 1 }], min_duration: 0 }),
  'Interface speed changed')
for (const metric of ['if_error_rate_pct', 'if_discard_rate_pct', 'if_flaps', 'if_speed_changed']) {
  assert.equal(M.ruleTakesOverrides({ metric }), true, `${metric} takes overrides`)
  assert.equal(M.ruleHasInstances({ metric }), true, `${metric} is per interface`)
}
assert.equal(M.holdWords(0), 'at once')
assert.equal(M.changeWords({ field: 'threshold', from: 90, to: 95 }), 'threshold 90 → 95')
assert.equal(M.changeWords({ field: 'min_duration', from: 300, to: 600 }), 'hold 5 min → 10 min')
assert.equal(M.overrideChangeWords({ threshold: 95, min_duration: 600, priority: 'P3' }), 'threshold 95, hold 10 min, priority P3')
assert.equal(M.overrideChangeWords({}), 'no change')

// Interface role rules the server refuses (interface_roles.validate).
assert.deepEqual(M.roleRuleProblems([{ role: 'uplink', field: 'if_type', pattern: '6,117' }, { role: 'wan', field: 'if_name', pattern: '(?i)^wan' }]), [])
assert.equal(M.roleRuleProblems([{ role: 'uplink', field: 'if_type', pattern: 'eth' }]).length, 1)
assert.equal(M.roleRuleProblems([{ role: 'uplink', field: 'if_name', pattern: '(' }]).length, 1)
assert.equal(M.roleRuleProblems([{ role: 'nope', field: 'if_name', pattern: 'x' }]).length, 1)
assert.equal(M.roleRuleProblems([{ role: 'access', field: 'if_alias', pattern: ' ' }]).length, 1)
assert.deepEqual([...M.INTERFACE_ROLES], ['uplink', 'access', 'wan', 'server', 'tunnel', 'vlan', 'loopback', 'management'])

// Wiring.
const read = (f) => fs.readFileSync(f, 'utf8')
const tpl = read('src/pages/templates/TemplateDetailPage.tsx')
assert.match(tpl, /<TabsTrigger value="recommended">Recommended alerts<\/TabsTrigger>/)
assert.match(tpl, /<InterfaceRolesEditor roles=\{work\.interface_roles\}/)
assert.match(tpl, /contentOf\(t\.draft \|\| t, t\)/, 'a draft without roles keeps the published roles and rule pack')
const api = read('src/pages/templates/api.ts')
assert.match(api, /rule_pack: JSON\.parse\(JSON\.stringify\(pick\('rule_pack'\)\)\)/, 'the rule pack round-trips unchanged')
assert.match(api, /encodeURIComponent\(key\)/, 'recommendation keys with ":" are encoded')
const rec = read('src/pages/templates/RecommendedAlerts.tsx')
assert.match(rec, /Nothing is created until you accept a recommendation; a later template revision never changes an accepted rule\./)
assert.match(rec, /Accepted on \{acc!\.template_name/)
assert.match(rec, /can\('alerts\.manage'\)/)
const roles = read('src/pages/templates/InterfaceRolesEditor.tsx')
assert.match(roles, /First match wins; an operator&apos;s role on an interface wins over the template\./)
assert.match(roles, /Uses the parent template&apos;s roles/)
const ovr = read('src/components/alerts/RuleOverrides.tsx')
assert.match(ovr, /Device and server rows are tried before group rows; the first matching row applies\. To turn a rule off\s+for one asset use an exclusion\./)
assert.match(ovr, /overrideApi\.reorder\(rule!\.id\), \{ ids \}/)
const rulesPage = read('src/pages/AlertRulesPage.tsx')
assert.match(rulesPage, /ruleTakesOverrides\(r\) && \(/)
assert.match(rulesPage, /<RuleOverridesDialog/)
assert.match(read('src/pages/device/ConfigurationTab.tsx'), /<EffectiveThresholdsCard kind="device" id=\{device\.id\} \/>/)
assert.match(read('src/components/alerts/EffectiveThresholdsCard.tsx'), /'\/alert-rules\/effective', \{ params: \{ asset_kind: kind, asset_id: id \} \}/)
const ifs = read('src/pages/device/InterfacesTab.tsx')
assert.match(ifs, /api\.put\(`\/devices\/\$\{deviceId\}\/interfaces\/\$\{ifIndex\}\/role`, \{ role \}\)/)
assert.match(ifs, /useCan\(\)\('devices\.manage'\)/)
const wiz = read('src/components/forms/AlertRuleWizardDialog.tsx')
assert.match(wiz, /<SelectItem value="tpl_">Template metric…<\/SelectItem>/)
assert.match(wiz, /profile_id: isDevice && templateScopeApplies\(s\.conditions\) \? s\.profile_id \|\| null : null/)
assert.match(wiz, /if_role: isDevice && interfaceRoleApplies\(s\.conditions\) \? s\.if_role \|\| null : null/)
assert.match(read('src/pages/groups/GroupDetailPage.tsx'), /Override on \$\{x\.name\}/)
assert.match(read('src/components/devices/TemplateInsightsSection.tsx'), /levels from alert rule/)

console.log('rule overrides UI: ok (overridable rules, words, role rules, wiring)')
