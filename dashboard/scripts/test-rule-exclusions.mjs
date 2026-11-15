// Shared alert rules turned off for one device or server (migrate-187, docs/alert-rule-exclusions.md).
// The field report: the switch on a server's Alerts tab flipped a global rule ("Server restart") off for every
// server. A shared rule's switch on an asset page now creates or removes an exclusion for that asset only; the
// rule's own toggle is reserved for rules pinned to the asset. This pins the switch decision table, the wording,
// and that every asset-page entry point goes through it.
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

const M = await load(`export * from './src/components/alerts/exclusionModel'`)

const ex = { created_by_name: 'Sara', created_at: '2026-11-12T14:05:00Z', reason: null }
const shared = { name: 'Server restart', enabled: true, shared: true, excluded: null }

// ── The switch decision table ─────────────────────────────────────────────────
let sw = M.ruleSwitch(shared, 'server', true)
assert.equal(sw.checked, true)
assert.equal(sw.action, 'exclude', 'a shared rule switches off for this server only, never globally')
assert.equal(sw.disabled, false)
assert.match(sw.label, /for this server only/)
assert.equal(sw.note, null)

sw = M.ruleSwitch({ ...shared, excluded: ex }, 'server', true)
assert.equal(sw.checked, false)
assert.equal(sw.action, 'include', 're-enabling removes the exclusion')
assert.match(sw.note, /^Disabled for this server by Sara · /)

sw = M.ruleSwitch({ ...shared, enabled: false }, 'device', true)
assert.equal(sw.checked, false)
assert.equal(sw.disabled, true, 'a rule that is off for everyone cannot be turned on from one asset')
assert.equal(sw.action, 'none')
assert.equal(sw.note, 'Off for every device — turn it on in Alert Rules')

sw = M.ruleSwitch({ ...shared, enabled: false, excluded: ex }, 'device', true)
assert.equal(sw.action, 'none', 'globally off wins over an exclusion')

sw = M.ruleSwitch({ ...shared, shared: false }, 'server', true)
assert.equal(sw.action, 'toggle', "the asset's own rule toggles itself")
assert.equal(sw.checked, true)

for (const rule of [shared, { ...shared, excluded: ex }, { ...shared, shared: false }]) {
  const ro = M.ruleSwitch(rule, 'server', false)
  assert.equal(ro.action, 'none', 'without alerts.manage nothing changes')
  assert.equal(ro.disabled, true)
}

// ── Wording and links ─────────────────────────────────────────────────────────
assert.equal(M.exclusionNote({ created_by_name: null, created_at: null, reason: 'lab box' }, 'device'),
  'Disabled for this device — lab box')
assert.equal(M.exclusionHref({ asset_kind: 'device', asset_id: 'd1' }), '/devices/d1')
assert.equal(M.exclusionHref({ asset_kind: 'server', asset_id: 's1' }), '/servers/s1')
assert.equal(M.exclusionAssetLabel({ asset_kind: 'server', asset_name: null, asset_ip: '10.0.0.5' }), '10.0.0.5')
assert.equal(M.exclusionAssetLabel({ asset_kind: 'device', asset_name: null, asset_ip: null }), 'Device')
assert.equal(M.exclusionBadge(0), null)
assert.equal(M.exclusionBadge(undefined), null)
assert.equal(M.exclusionBadge(3), 'Off for 3')
assert.match(M.excludeConsequence('server'), /every other server/)
assert.match(M.excludeConsequence('server'), /without a recovery notice/)
assert.equal(M.exclusionApi.remove('r1', 'device', 'd1'), '/alert-rules/r1/exclusions/device/d1')
assert.equal(M.exclusionApi.forAsset, '/alert-rules/for-asset')
assert.equal(M.formatWhen('not a date'), 'not a date')

// ── Every asset-page entry point uses the exclusion, not the global toggle ────
const read = (p) => fs.readFileSync(path.join(src, p), 'utf8')
const serverTab = read('pages/servers/detail/tabs/AlertsTab.tsx')
assert.match(serverTab, /ruleSwitch\(/, 'the server Alerts tab decides its switch with ruleSwitch')
assert.match(serverTab, /sw\.action === 'exclude'/)
assert.doesNotMatch(serverTab, /onCheckedChange=\{\(\) => toggleRule\.mutate\(r\.id\)\}/,
  'the server tab switch must not toggle a global rule for every server')
assert.match(serverTab, /<ExcludeRuleDialog/)

const config = read('pages/device/ConfigurationTab.tsx')
assert.match(config, /<AssetAlertRules kind="device"/, 'the device page lists the rules that apply to it')

const ui = read('components/alerts/RuleExclusions.tsx')
assert.match(ui, /ruleSwitch\(r, kind, canManage\)/)
assert.match(ui, /exclusionHref\(x\)/, "the rule's exclusion list links each asset")
assert.match(ui, /Re-enable/)

const rulesPage = read('pages/AlertRulesPage.tsx')
assert.match(rulesPage, /<RuleExclusionsDialog/, 'Alert Rules lists the assets a rule is off for')
assert.match(rulesPage, /exclusionBadge\(r\.exclusion_count\)/)

const policy = read('components/alerts/PolicyCard.tsx')
assert.match(policy, /Off for this \{asset!\.kind\}/, 'the alert page offers the per-asset switch next to the global one')
assert.match(read('pages/AlertDetailPage.tsx'), /asset=\{alert\.device_id \?/)

// The rem type scale only (docs/design-system.md): no pixel font sizes in the new UI.
for (const file of ['components/alerts/RuleExclusions.tsx', 'components/alerts/exclusionModel.ts']) {
  assert.doesNotMatch(read(file), /text-\[\d+px\]/, `${file} uses a pixel font size`)
}

console.log('rule exclusions: ok')
