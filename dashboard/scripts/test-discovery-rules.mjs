// Discovery rules (phase 1, migrate-188; phase 2 on inventory, migrate-191; phase 3 re-checks, children, approval,
// export / import and starter rules, migrate-193; phase 4 interfaces, sensors, agent policies, service check templates,
// schedules and the Unmatched report, no migration): the pure model behind the rule builder, the rule list and the scan-result
// Rule / Decision columns (src/pages/discovery/ruleModel.ts). Pins the tree edits and their depth / leaf limits, the
// one-sentence condition and action summaries, the disposition wording, the preview summary and the reorder helper,
// and that the pages, routes, tab and sidebar entry are wired.
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

const M = await load(`export * from './src/pages/discovery/ruleModel'`)

// A catalog shaped like GET /discovery-v2/rules/catalog (subset).
const op = (key, label, value) => ({ key, label, value })
const catalog = {
  fields: [
    { key: 'ip_address', type: 'ip', label: 'IP address', help: '', values: null, source: null,
      operators: [op('in_subnet', 'is in subnet', 'cidr_list'), op('in_range', 'is in range', 'ip_range'), op('equals', 'equals', 'text')] },
    { key: 'sys_object_id', type: 'oid', label: 'sysObjectID', help: '', values: null, source: null,
      operators: [op('equals', 'equals', 'text'), op('starts_with', 'is under (OID subtree)', 'text'), op('is_empty', 'is empty', 'none')] },
    { key: 'vendor', type: 'text', label: 'Vendor', help: '', values: null, source: null,
      operators: [op('equals', 'equals', 'text'), op('contains', 'contains', 'text'), op('in_list', 'is one of', 'list'), op('matches', 'matches regex', 'text')] },
    { key: 'open_ports', type: 'set', label: 'Open TCP ports', help: '', values: null, source: null,
      operators: [op('has_any', 'has any of', 'list'), op('has_all', 'has all of', 'list')] },
    { key: 'confidence_score', type: 'number', label: 'Identification confidence', help: '', values: null, source: null,
      operators: [op('gte', '≥', 'number'), op('between', 'is between', 'range')] },
    { key: 'credential_status', type: 'enum', label: 'Credential status', help: '', values: ['valid', 'invalid'], source: null,
      operators: [op('is', 'is', 'text'), op('is_any_of', 'is any of', 'list')] },
    { key: 'profile_id', type: 'reference', label: 'Discovery profile', help: '', values: null, source: 'discovery_profiles',
      operators: [op('is', 'is', 'ref'), op('is_any_of', 'is any of', 'ref_list')] },
    { key: 'is_server', type: 'boolean', label: 'Server-class host', help: '', values: null, source: null,
      operators: [op('is_true', 'is true', 'none')] },
  ],
  groups: [{ key: 'all', label: 'All of' }, { key: 'any', label: 'Any of' }, { key: 'none', label: 'None of' }],
  actions: [
    { key: 'decision', label: 'Import decision', kind: 'single', value: 'decision', permission: ['discovery.run'], asset_kinds: ['device', 'server'], source: null, help: '', min: null, max: null },
    { key: 'group', label: 'Device group', kind: 'single', value: 'ref', permission: ['devices.manage'], asset_kinds: ['device'], source: 'device_groups', help: '', min: null, max: null },
    { key: 'ping_interval', label: 'Ping interval (seconds)', kind: 'single', value: 'int', permission: ['devices.manage'], asset_kinds: ['device'], source: null, help: '', min: 10, max: 3600 },
    { key: 'tags', label: 'Add tags', kind: 'additive', value: 'tags', permission: ['devices.manage'], asset_kinds: ['device', 'server'], source: null, help: '', min: null, max: null },
  ],
  decisions: ['auto_import', 'review', 'ignore'],
  import_as: ['auto', 'device', 'server', 'both'],
  device_types: ['router', 'switch'],
  limits: { max_depth: 3, max_leaves: 50, max_regex: 200 },
}
const lookups = {
  device_groups: { 'aaaaaaaa-0000-0000-0000-000000000001': 'Datacenter firewalls' },
  discovery_profiles: { 'bbbbbbbb-0000-0000-0000-000000000001': 'DC sweep' },
}

// ── defaults ─────────────────────────────────────────────────────────────────
const d = M.defaultRule()
assert.equal(d.mode, 'draft', 'a new rule starts as a draft')
assert.deepEqual(d.asset_kinds, ['device'])
assert.deepEqual(d.condition, { op: 'all', conditions: [] })
assert.deepEqual(d.actions, [{ key: 'decision', value: 'review' }], 'the safe default decision is keep for review')
assert.equal(d.stop_processing, false)
assert.equal(d.profile_ids, null)

assert.deepEqual(M.defaultLeaf(catalog), { field: 'ip_address', operator: 'in_subnet', value: [] })
assert.deepEqual(M.defaultLeaf(catalog, 'vendor'), { field: 'vendor', operator: 'equals', value: '' })
for (const [kind, value] of [['none', null], ['text', ''], ['list', []], ['cidr_list', []], ['ref_list', []], ['number', 0], ['range', [0, 0]], ['ip_range', ''], ['ref', '']]) {
  assert.deepEqual(M.defaultValueFor(kind), value, `default value for ${kind}`)
}

// Field / operator switches keep what still makes sense.
let leaf = { field: 'vendor', operator: 'equals', value: 'Fortinet' }
assert.deepEqual(M.changeLeafField(leaf, 'sys_object_id', catalog), { field: 'sys_object_id', operator: 'equals', value: '' },
  'the operator survives a field change; a value of another field type does not')
assert.deepEqual(M.changeLeafField(leaf, 'ip_address', catalog), { field: 'ip_address', operator: 'equals', value: '' })
assert.deepEqual(M.changeLeafField(leaf, 'open_ports', catalog), { field: 'open_ports', operator: 'has_any', value: [] })
assert.deepEqual(M.changeLeafOperator(leaf, 'contains', catalog), { field: 'vendor', operator: 'contains', value: 'Fortinet' },
  'same value kind keeps the value')
assert.deepEqual(M.changeLeafOperator(leaf, 'in_list', catalog), { field: 'vendor', operator: 'in_list', value: [] })
assert.equal(M.changeLeafOperator(leaf, 'bogus', catalog), leaf, 'an unknown operator changes nothing')

// ── tree edits ───────────────────────────────────────────────────────────────
let root = { op: 'all', conditions: [] }
const L = (field, operator, value) => ({ field, operator, value })
root = M.addLeaf(root, [], L('sys_object_id', 'starts_with', '1.3.6.1.4.1.12356'))
root = M.addLeaf(root, [], L('ip_address', 'in_subnet', ['10.51.0.0/16']))
assert.equal(M.countLeaves(root), 2)
assert.equal(M.treeDepth(root), 1)
root = M.addGroup(root, [], 'any', L('vendor', 'equals', 'Fortinet'))
assert.equal(M.treeDepth(root), 2)
assert.deepEqual(M.getNode(root, [2]), { op: 'any', conditions: [L('vendor', 'equals', 'Fortinet')] },
  'a new group starts with one condition (an empty "any" would match nothing)')
root = M.addLeaf(root, [2], L('vendor', 'equals', 'Cisco'))
assert.equal(M.countLeaves(root), 4)
assert(M.canAddGroup(root, [2]), 'level 2 may hold a level-3 group')
root = M.addGroup(root, [2], 'all', L('is_server', 'is_true', null))
assert.equal(M.treeDepth(root), 3)
assert(!M.canAddGroup(root, [2, 2]), 'groups nest at most 3 levels')
assert.equal(M.addGroup(root, [2, 2], 'any', L('vendor', 'equals', 'x')), root, 'adding past the depth limit is a no-op')
assert(!M.canAddGroup(root, [0]), 'a leaf cannot hold a group')

// Immutability: edits return new objects and leave the input alone.
const before = JSON.stringify(root)
const edited = M.updateNode(root, [2, 0], (n) => ({ ...n, value: 'Juniper' }))
assert.equal(JSON.stringify(root), before)
assert.equal(M.getNode(edited, [2, 0]).value, 'Juniper')
assert.equal(edited.conditions[0], root.conditions[0], 'untouched branches are shared')
assert.equal(M.setGroupOp(root, [2], 'none').conditions[2].op, 'none')
assert.equal(M.setGroupOp(root, [], 'any').op, 'any')

let removed = M.removeNode(root, [2, 1])
assert.equal(M.getNode(removed, [2]).conditions.length, 2)
removed = M.removeNode(root, [2])
assert.equal(removed.conditions.length, 2)
assert.deepEqual(M.removeNode(root, []), { op: 'all', conditions: [] }, 'removing the root clears it')
assert.equal(M.getNode(root, [9, 9]), undefined)
assert.equal(M.updateNode(root, [9], (n) => n), root, 'a missing path changes nothing')

// Leaf limit.
let big = { op: 'all', conditions: [] }
for (let i = 0; i < 60; i++) big = M.addLeaf(big, [], L('vendor', 'equals', `v${i}`))
assert.equal(M.countLeaves(big), 50, 'at most 50 conditions')
assert(!M.canAddLeaf(big))
assert(!M.canAddGroup(big, []), 'no group either once the leaf limit is reached')
assert.equal(M.countLeaves(M.addLeaf(big, [], L('vendor', 'equals', 'x'), { ...M.DEFAULT_LIMITS, max_leaves: 51 })), 51)

// ── values ───────────────────────────────────────────────────────────────────
assert.deepEqual(M.parseList(' 22, 443,\n8443 ,, 22 '), ['22', '443', '8443'])
assert.deepEqual(M.parseList(''), [])
assert.equal(M.listText(['a', 'b']), 'a, b')

// ── condition summaries ──────────────────────────────────────────────────────
const two = { op: 'all', conditions: [L('sys_object_id', 'starts_with', '1.3.6.1.4.1.12356'), L('ip_address', 'in_subnet', ['10.51.0.0/16'])] }
assert.equal(M.summarizeCondition(two, catalog),
  'sysObjectID is under 1.3.6.1.4.1.12356 and IP address is in subnet 10.51.0.0/16')
assert.equal(M.summarizeCondition({ op: 'all', conditions: [] }, catalog), 'every discovered asset', 'an empty "all" is a catch-all')
assert.equal(M.summarizeCondition({ op: 'any', conditions: [] }, catalog), 'matches nothing')
assert.equal(M.summarizeCondition({ op: 'any', conditions: [L('vendor', 'equals', 'Fortinet'), L('vendor', 'contains', 'Cisco')] }, catalog),
  'Vendor equals Fortinet or Vendor contains Cisco')
assert.equal(M.summarizeCondition({ op: 'none', conditions: [L('vendor', 'equals', 'HP'), L('is_server', 'is_true', null)] }, catalog),
  'not (Vendor equals HP or Server-class host is true)')
assert.equal(M.summarizeCondition({
  op: 'all',
  conditions: [L('ip_address', 'in_subnet', ['10.0.0.0/8', '172.16.0.0/12']), { op: 'any', conditions: [L('vendor', 'equals', 'Fortinet'), L('vendor', 'equals', 'Cisco')] }],
}, catalog), 'IP address is in subnet 10.0.0.0/8 or 172.16.0.0/12 and (Vendor equals Fortinet or Vendor equals Cisco)',
'nested groups are parenthesised')
assert.equal(M.summarizeLeaf(L('vendor', 'in_list', ['Fortinet', 'Cisco', 'Juniper']), catalog), 'Vendor is one of Fortinet, Cisco or Juniper')
assert.equal(M.summarizeLeaf(L('open_ports', 'has_all', ['22', '443']), catalog), 'Open TCP ports has all of 22 and 443')
assert.equal(M.summarizeLeaf(L('confidence_score', 'gte', 80), catalog), 'Identification confidence ≥ 80')
assert.equal(M.summarizeLeaf(L('confidence_score', 'between', [50, 90]), catalog), 'Identification confidence is between 50 and 90')
assert.equal(M.summarizeLeaf(L('sys_object_id', 'is_empty', null), catalog), 'sysObjectID is empty')
assert.equal(M.summarizeLeaf(L('profile_id', 'is', 'bbbbbbbb-0000-0000-0000-000000000001'), catalog, lookups), 'Discovery profile is DC sweep',
  'reference values show their names')
assert.equal(M.summarizeLeaf(L('profile_id', 'is', 'cccccccc-0000-0000-0000-000000000009'), catalog, lookups), 'Discovery profile is cccccccc…',
  'an unknown reference shows a short id')
assert.equal(M.summarizeLeaf(L('unknown_field', 'eq', 'x'), catalog), 'unknown_field eq x', 'unknown fields fall back to their keys')
assert.equal(M.sentenceCase('sysObjectID is under 1.3'), 'sysObjectID is under 1.3', 'mixed-case names keep their spelling')
assert.equal(M.sentenceCase('every discovered asset'), 'Every discovered asset')
assert.equal(M.plainOperator('is under (OID subtree)'), 'is under')

// Client-side problems (the server validates again).
assert.deepEqual(M.conditionProblems(two, catalog), [])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('vendor', 'equals', ' ')] }, catalog), ['Vendor equals: a value is required'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('ip_address', 'in_subnet', [])] }, catalog), ['IP address is in subnet: give at least one value'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('confidence_score', 'between', [90, 50])] }, catalog),
  ['Identification confidence is between: the first number must not exceed the second'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('confidence_score', 'gte', NaN)] }, catalog), ['Identification confidence ≥: a number is required'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('vendor', 'matches', 'x'.repeat(201))] }, catalog), ['Vendor matches regex: at most 200 characters'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('nope', 'equals', 'x')] }, catalog), ['Choose a field and a test for every condition'])

// ── action summaries ─────────────────────────────────────────────────────────
const actions = [
  { key: 'tags', value: ['dc', 'firewall'] },
  { key: 'group', value: 'aaaaaaaa-0000-0000-0000-000000000001' },
  { key: 'decision', value: 'auto_import', import_as: 'device' },
  { key: 'ping_interval', value: 30 },
]
assert.deepEqual(M.summarizeActions(actions, lookups, catalog), [
  'Auto-import as network device', 'Tags: dc, firewall', 'Group: Datacenter firewalls', 'Ping every 30 s',
], 'the decision comes first')
assert.equal(M.summarizeAction({ key: 'decision', value: 'auto_import' }), 'Auto-import as network device', 'import_as defaults to device')
assert.equal(M.summarizeAction({ key: 'decision', value: 'auto_import', import_as: 'auto' }), 'Auto-import as auto-routed')
assert.equal(M.summarizeAction({ key: 'decision', value: 'review' }), 'Keep for review')
assert.equal(M.summarizeAction({ key: 'decision', value: 'ignore' }), 'Ignore')
assert.equal(M.summarizeAction({ key: 'device_type', value: 'firewall' }), 'Type: firewall')
assert.equal(M.summarizeAction({ key: 'snmp_interval', value: 300 }), 'SNMP poll every 300 s')
assert.equal(M.summarizeAction({ key: 'location', value: 'DC1' }), 'Location: DC1')
assert.equal(M.summarizeAction({ key: 'future_action', value: 'x' }), 'future_action: x')

assert.deepEqual(M.defaultActionValue({ key: 'decision', value: 'decision' }), { key: 'decision', value: 'review' })
assert.deepEqual(M.defaultActionValue({ key: 'tags', value: 'tags' }), { key: 'tags', value: [] })
assert.deepEqual(M.defaultActionValue({ key: 'ping_interval', value: 'int', min: 10 }), { key: 'ping_interval', value: 60 })
assert.deepEqual(M.defaultActionValue({ key: 'snmp_interval', value: 'int', min: 30 }), { key: 'snmp_interval', value: 300 })

assert.deepEqual(M.actionProblems({ actions: [], asset_kinds: ['device'] }, catalog), ['Add at least one action'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'group', value: 'x' }], asset_kinds: ['server'] }, catalog),
  ['Device group applies to network devices only'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'ping_interval', value: 5 }], asset_kinds: ['device'] }, catalog),
  ['Ping interval (seconds): between 10 and 3600'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'tags', value: [] }], asset_kinds: ['device'] }, catalog), ['Add tags: give at least one tag'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'decision', value: 'review' }, { key: 'decision', value: 'ignore' }], asset_kinds: ['device'] }, catalog),
  ['Import decision: only once per rule'])
assert.deepEqual(M.ruleProblems({ ...M.defaultRule(), name: ' ' }, catalog), ['Give the rule a name'])
assert.deepEqual(M.ruleProblems({ ...M.defaultRule(), name: 'x', asset_kinds: [] }, catalog)[0],
  'Choose what the rule looks at: network devices, servers or controller-managed children')

// ── dispositions, outcomes and the preview sentence ──────────────────────────
assert.equal(M.dispositionText('review', 'license_capacity_reached'), 'Kept for review — license capacity reached')
assert.equal(M.dispositionText('would_import', 'profile_review'), 'Would import — the profile only lets rules annotate')
assert.equal(M.dispositionText('imported'), 'Imported')
assert.equal(M.dispositionLabel(null), 'Not evaluated', 'null disposition: the rules never ran')
assert.equal(M.dispositionLabel('something_new'), 'something new')
assert.equal(M.reasonText('cap'), 'per-scan auto-import limit reached')
for (const k of ['imported', 'ignored', 'review', 'would_import', 'would_ignore', 'existing', 'no_match', 'auto_import']) {
  assert(M.DISPOSITION_LABEL[k] && M.DISPOSITION_TONE[k], `disposition ${k} has a label and a tone`)
}
for (const k of ['license_capacity_reached', 'cap', 'not_ready', 'profile_review', 'profile_ignore_only', 'conflict', 'error']) {
  assert(M.REASON_TEXT[k], `reason ${k} has text`)
}
for (const k of ['applied', 'simulated', 'shadowed', 'blocked_license', 'blocked_cap', 'not_ready', 'error']) {
  assert(M.OUTCOME_LABEL[k] && M.OUTCOME_TONE[k], `outcome ${k} has a label and a tone`)
}
assert.equal(M.dispositionTone('imported'), 'success')
assert.equal(M.MODE_LABEL.active, 'Active')

assert.equal(
  M.summarizePreview({ evaluated: 318, matched: 42, auto_import: 10, would_import: 7, ignore: 3, review: 22, no_match: 276 }, 12),
  'matches 42 of 318: would import 17, ignore 3, review 22; license room 12',
)
assert.equal(M.summarizePreview({ evaluated: 5, matched: 0, no_match: 5 }, null), 'matches 0 of 5; license room unlimited')
assert.equal(M.summarizePreview({ evaluated: 1, matched: 1, review: 1 }), 'matches 1 of 1: review 1', 'sample facts carry no license room')
assert.equal(
  M.summarizeCounts({ evaluated: 318, matched: 42, imported: 17, ignored: 3, review: 22, blocked_license: 2, errors: 0 }),
  'Evaluated 318 · matched 42 · imported 17 · ignored 3 · kept for review 22 · blocked by license 2',
)
assert.equal(M.traceLeafText(['sys_object_id', 'starts_with', true], catalog), 'sysObjectID is under ✓')
assert.equal(M.traceLeafText(['vendor', 'equals', false], catalog), 'Vendor equals ✗')

// ── reorder ──────────────────────────────────────────────────────────────────
const ids = ['a', 'b', 'c']
assert.deepEqual(M.moveItem(ids, 1, -1), ['b', 'a', 'c'])
assert.deepEqual(M.moveItem(ids, 1, 1), ['a', 'c', 'b'])
assert.equal(M.moveItem(ids, 0, -1), ids, 'moving the first rule up is a no-op')
assert.equal(M.moveItem(ids, 2, 1), ids)
assert.deepEqual(ids, ['a', 'b', 'c'], 'the input list is not changed')

// ── import-mode wording (wizard and preview share it) ────────────────────────
assert.match(M.IMPORT_MODE_TEXT.review.label, /only annotate/)
assert.match(M.IMPORT_MODE_TEXT.auto_match.label, /auto-import and ignore/)
assert.match(M.IMPORT_MODE_TEXT.ignore_match.label, /ignore only/)

// ── wiring ───────────────────────────────────────────────────────────────────
const read = (rel) => fs.readFileSync(path.join(process.cwd(), rel), 'utf8')
const app = read('src/App.tsx')
for (const route of ['"rules"', '"rules/new"', '"rules/:id"']) {
  assert(app.includes(`<Route path=${route}`), `App.tsx declares the discovery route ${route}`)
}
assert.match(read('src/components/layout/navigation.ts'), /\{ to: '\/discovery\/rules', label: 'Rules', icon: ListChecks \}/,
  'the sidebar lists Discovery → Rules (discovery.view from its parent)')
assert.match(read('src/pages/discovery/DiscoveryLayout.tsx'), /to: '\/discovery\/rules'/, 'the Discovery tab strip has Rules')
const wizard = read('src/pages/discovery/WizardPage.tsx')
assert.match(wizard, /IMPORT_MODE_TEXT/, 'the wizard uses the shared import-mode wording')
assert.match(wizard, /\/discovery\/rules/, 'the wizard links to Discovery → Rules')
const runPage = read('src/pages/discovery/RunPage.tsx')
assert.match(runPage, /hasRules && <Th>Rule<\/Th>/, 'the Rule column appears only when rules ran')
assert.match(runPage, /can\('discovery\.run'\)/, 'Re-evaluate rules needs discovery.run')
const rulesPage = read('src/pages/discovery/RulesPage.tsx')
assert.match(rulesPage, /can\('discovery\.manage_rules'\)/, 'the rule list is read-only without discovery.manage_rules')
assert.match(read('src/pages/discovery/RuleEditorPage.tsx'), /expected_revision|update\(id!, draft, revision\)/, 'edits send the loaded revision')
assert.match(read('src/pages/discovery/api.ts'), /expected_revision: expectedRevision/)
for (const file of ['ruleModel.ts', 'ruleUi.tsx', 'RulesPage.tsx', 'RuleEditorPage.tsx']) {
  assert(!/text-\[\d+px\]/.test(read(`src/pages/discovery/${file}`)), `${file} uses the rem type scale, never pixel font sizes`)
}

// ── phase 2: modes, triggers, enforcement, field sources, new actions ───────
assert.deepEqual(d.triggers, ['discovery'], 'a new rule runs on scan results')
assert.equal(d.enforcement, 'set_once', 'a new rule keeps later hand edits')
assert.equal(M.MODE_LABEL.simulate, 'Simulating')
assert.equal(M.MODE_TONE.simulate, 'info')
assert.deepEqual(M.MODE_ORDER, ['draft', 'simulate', 'active', 'disabled'])
for (const m of M.MODE_ORDER) assert(M.MODE_HELP[m], `mode ${m} has a one-line explanation`)
assert.match(M.MODE_HELP.simulate, /changes nothing/)
assert.deepEqual(M.ruleProblems({ ...M.defaultRule(), name: 'x', triggers: [] }, catalog),
  ['Choose when the rule runs: on scan results, on new devices and servers, whenever they change, or at the scheduled times'])
assert.equal(M.ruleRunsText({ triggers: ['discovery'], enforcement: 'set_once' }), 'runs on scans · set once')
assert.equal(M.ruleRunsText({ triggers: ['discovery', 'created'], enforcement: 'enforce' }), 'runs on scans and new devices and servers · enforced')
assert.equal(M.ruleRunsText({}), 'runs on scans · set once', 'a phase 1 rule runs on scans, set once')
assert.equal(M.TRIGGER_LABEL.created, 'Devices and servers when they are added')
assert(M.ENFORCEMENT_HELP.set_once && M.ENFORCEMENT_HELP.enforce)

assert.equal(M.fieldSourceHint({ applies_to: ['scan'] }), 'scan only')
assert.equal(M.fieldSourceHint({ applies_to: ['server'] }), 'servers only')
assert.equal(M.fieldSourceHint({ applies_to: ['device', 'server'] }), 'devices and servers')
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server'] }), '', 'a field known everywhere needs no hint')
assert.equal(M.fieldSourceHint({}), 'scan only', 'a phase 1 catalog (no applies_to) means scan results only')

const catalog2 = {
  ...catalog,
  actions: [
    ...catalog.actions,
    { key: 'owner', label: 'Owner', kind: 'single', value: 'text', permission: ['devices.manage'], asset_kinds: ['device', 'server'], source: null, help: '', min: null, max: null },
    { key: 'environment', label: 'Server environment', kind: 'single', value: 'text', permission: ['servers.manage'], asset_kinds: ['server'], source: null, help: '', min: null, max: null },
    { key: 'ping_enabled', label: 'Turn ping monitoring on', kind: 'single', value: 'on', permission: ['devices.manage'], asset_kinds: ['device'], source: null, help: '', min: null, max: null },
  ],
}
assert.deepEqual(M.defaultActionValue({ key: 'ping_enabled', value: 'on' }), { key: 'ping_enabled', value: true }, 'the only value of an "on" action is true')
assert.deepEqual(M.actionProblems({ actions: [{ key: 'ping_enabled', value: false }], asset_kinds: ['device'] }, catalog2),
  ['Turn ping monitoring on: a rule can only turn it on'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'ping_enabled', value: true }], asset_kinds: ['device'] }, catalog2), [])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'environment', value: 'prod' }], asset_kinds: ['device'] }, catalog2),
  ['Server environment applies to servers only'])
assert.equal(M.summarizeAction({ key: 'ping_enabled', value: true }), 'Turn ping on')
assert.equal(M.summarizeAction({ key: 'owner', value: 'NOC' }), 'Owner: NOC')
assert.equal(M.summarizeAction({ key: 'environment', value: 'production' }), 'Environment: production')

// Activation permissions mirror actions.permissions_needed on the server.
assert.deepEqual(M.activationPermissions([{ key: 'tags', value: ['a'] }], ['device'], catalog2), ['devices.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'tags', value: ['a'] }], ['server'], catalog2), ['servers.manage'],
  'tags on servers need servers.manage, not devices.manage')
assert.deepEqual(M.activationPermissions([{ key: 'owner', value: 'x' }], ['device', 'server'], catalog2), ['devices.manage', 'servers.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'group', value: 'g' }], ['server'], catalog2), [], 'a device-only action on a server-only rule needs nothing')
assert.deepEqual(M.activationPermissions([{ key: 'decision', value: 'auto_import', import_as: 'auto' }], ['device'], catalog2),
  ['discovery.run', 'servers.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'decision', value: 'review' }], ['device'], catalog2), ['discovery.run'])

// ── phase 2: run on inventory ────────────────────────────────────────────────
for (const k of ['applied', 'kept_manual_edit', 'not_reversible', 'skipped_changed', 'reverted', 'unchanged', 'simulated']) {
  assert(M.OUTCOME_LABEL[k] && M.OUTCOME_TONE[k], `history outcome ${k} has a label and a tone`)
}
for (const k of ['apply', 'kept_manual_edit', 'not_reversible']) {
  assert(M.CHANGE_OUTCOME_LABEL[k] && M.CHANGE_OUTCOME_TONE[k], `change outcome ${k} has a label and a tone`)
}
assert.equal(
  M.summarizeInventoryPreview({ assets: 120, matched: 42, changes: 17, assets_changed: 9, kept_manual_edit: 3, not_reversible: 0 }),
  '42 devices and servers match; 17 values would change on 9 assets; 3 hand edits kept',
)
assert.equal(M.summarizeInventoryPreview({ matched: 1, changes: 1, assets_changed: 1 }, ['device']), '1 device matches; 1 value would change on 1 asset')
assert.equal(M.summarizeInventoryPreview({ matched: 0, changes: 0 }, ['server']), '0 servers match; nothing would change')
assert.equal(M.summarizeInventoryPreview({ matched: 5, changes: 0, kept_manual_edit: 1, not_reversible: 2 }, ['device', 'server']),
  '5 devices and servers match; nothing would change; 1 hand edit kept; 2 left alone (not reversible)')
assert.equal(M.summarizeApplyResult({ applied: 17, kept_manual_edit: 3, not_reversible: 0, skipped_changed: 1 }),
  'Changed 17 values · 3 hand edits kept · 1 skipped (changed since the preview)')
assert.equal(M.summarizeApplyResult({ applied: 1 }), 'Changed 1 value')
assert.equal(M.summarizeUndoResult({ reverted: 4, skipped_changed: 1, not_reversible: 0, imported_devices: [] }),
  'Reverted 4 values · 1 left as they are (changed since)')
assert.equal(M.summarizeUndoResult({ reverted: 0, imported_devices: [{ device_id: 'x' }, { device_id: 'y' }] }),
  'Reverted 0 values · 2 devices imported by the scan')

assert(M.canUndoRun({ mode: 'apply', status: 'completed', trigger: 'manual' }))
assert(M.canUndoRun({ mode: 'apply', status: 'partial', trigger: 'discovery' }))
assert(M.canUndoRun({ mode: 'apply', status: 'completed', trigger: 'created' }))
assert(!M.canUndoRun({ mode: 'apply', status: 'undone', trigger: 'manual' }), 'a run is undone once')
assert(!M.canUndoRun({ mode: 'apply', status: 'completed', trigger: 'undo' }), 'an undo cannot be undone')
assert(!M.canUndoRun({ mode: 'simulate', status: 'completed', trigger: 'created' }), 'a simulation changed nothing')
assert(!M.canUndoRun({ mode: 'annotate', status: 'completed', trigger: 'discovery' }), 'an annotation changed nothing')
assert(!M.canUndoRun({ mode: 'apply', status: 'failed', trigger: 'manual' }))

assert.equal(M.runTriggerText({ trigger: 'discovery', mode: 'apply', discovery_run_id: 'r' }), 'Scan · apply')
assert.equal(M.runTriggerText({ trigger: 'manual', mode: 'annotate', discovery_run_id: 'r' }), 'Re-evaluated · annotate only')
assert.equal(M.runTriggerText({ trigger: 'manual', mode: 'apply', discovery_run_id: null }), 'Run on inventory · apply')
assert.equal(M.runTriggerText({ trigger: 'created', mode: 'simulate', discovery_run_id: null }), 'New device / server · simulate')
assert.equal(M.runTriggerText({ trigger: 'undo', mode: 'apply', discovery_run_id: null }), 'Undo')
assert.equal(M.runStatusTone('undone'), 'outline')

assert.equal(M.assetPath('device', 'abc'), '/devices/abc')
assert.equal(M.assetPath('server', 'abc'), '/servers/abc')
assert.equal(M.assetPath('discovery_result', 'abc'), null, 'scan results have no asset page')
assert.equal(M.assetPath('device', null), null)

const D1 = 'aaaaaaaa-0000-0000-0000-000000000001'
const D2 = 'AAAAAAAA-0000-0000-0000-000000000002'
const S1 = 'cccccccc-0000-0000-0000-000000000003'
assert.deepEqual(M.parseRunTargets(`?device=${D1}`), { device_ids: [D1], server_ids: [], child_ids: [], kinds: ['device'] })
assert.deepEqual(M.parseRunTargets(`?devices=${D1},${D2}, ${D1},not-an-id`),
  { device_ids: [D1, D2.toLowerCase()], server_ids: [], child_ids: [], kinds: ['device'] }, 'ids are de-duplicated, lower-cased and checked')
assert.deepEqual(M.parseRunTargets(`?server=${S1}`), { device_ids: [], server_ids: [S1], child_ids: [], kinds: ['server'] })
assert.deepEqual(M.parseRunTargets(`?device=${D1}&server=${S1}`), { device_ids: [D1], server_ids: [S1], child_ids: [], kinds: ['device', 'server'] })
assert.deepEqual(M.parseRunTargets(''), { device_ids: [], server_ids: [], child_ids: [], kinds: ['device', 'server'] }, 'nothing pre-selected: both kinds')
assert.deepEqual(M.parseRunTargets(new URLSearchParams({ devices: 'x,y' })), { device_ids: [], server_ids: [], child_ids: [], kinds: ['device', 'server'] })

assert(M.isSubnet('10.0.0.0/16'))
assert(M.isSubnet(' 192.168.1.0/24 '))
assert(M.isSubnet('fd00::/8'))
assert(!M.isSubnet('10.0.0.0'), 'a bare address is not a subnet')
assert(!M.isSubnet('10.0.0.0/33'))
assert(!M.isSubnet('300.0.0.0/8'))
assert(!M.isSubnet('fw-01/8'))

assert.deepEqual(M.buildTargets({ kinds: ['device', 'server'], tags: [], subnet: ' ', query: '' }), { kinds: ['device', 'server'] },
  'empty filters are left out')
assert.deepEqual(M.buildTargets({ kinds: ['server'], device_ids: [D1], server_ids: [S1], group_ids: ['g'], tags: ['dc'], subnet: '10.0.0.0/8 ', query: ' fw ' }),
  { kinds: ['server'], server_ids: [S1], group_ids: ['g'], tags: ['dc'], subnet: '10.0.0.0/8', query: 'fw' },
  'pre-selected ids follow the kinds that are ticked')

assert.equal(M.settingLabel('group', catalog), 'Device group')
assert.equal(M.settingLabel('tags', catalog), 'Tag')
assert.equal(M.settingLabel('ping_enabled', catalog2), 'Ping monitoring')
assert.equal(M.settingLabel('template', null), 'Monitoring template', 'labels without the catalog (devices.view only)')
assert.equal(M.settingLabel('something_new', null), 'Something new')
assert.equal(M.formatSettingValue('group', 'aaaaaaaa-0000-0000-0000-000000000001', lookups), 'Datacenter firewalls')
assert.equal(M.formatSettingValue('group', null, lookups), 'not set')
assert.equal(M.formatSettingValue('ping_enabled', true), 'On')
assert.equal(M.formatSettingValue('ping_enabled', false), 'Off')
assert.equal(M.formatSettingValue('ping_interval', 30), '30 s')
assert.equal(M.formatSettingValue('owner', 'NOC'), 'NOC')
assert.equal(M.changeText('group', null, 'aaaaaaaa-0000-0000-0000-000000000001', lookups), 'not set → Datacenter firewalls')
assert.equal(M.changeText('ping_enabled', false, true), 'Off → On')
assert.equal(M.changeText('tags', null, 'datacenter'), '+ datacenter', 'tags are only added')
assert.equal(M.changeText('tags', 'datacenter', null), '− datacenter', 'an undo removes the tag a rule added')
assert.equal(M.overriddenText({ rule_name: 'Branch', winner_rule_name: 'DC firewalls', action_key: 'group', assets: 12 }, catalog),
  'Branch: device group is overridden by DC firewalls on 12 assets')
assert.equal(M.plural(1, 'asset'), '1 asset')
assert.equal(M.plural(2, 'asset'), '2 assets')

// ── phase 2 wiring ───────────────────────────────────────────────────────────
const appSrc = read('src/App.tsx')
assert(appSrc.indexOf('<Route path="rules/run"') > 0, 'App.tsx declares /discovery/rules/run')
assert(appSrc.indexOf('<Route path="rules/run"') < appSrc.indexOf('<Route path="rules/:id"'), 'rules/run is declared before rules/:id')
const runSrc = read('src/pages/discovery/RuleRunPage.tsx')
assert.match(runSrc, /can\('discovery\.manage_rules'\)/, 'the run page explains that managing rules is needed')
assert.match(runSrc, /preview_stale/, 'a stale preview is refreshed')
assert.match(runSrc, /digest: preview!\.digest/, 'apply sends the digest of the preview the user saw')
assert.match(runSrc, /parseRunTargets\(params\)/, 'the run page takes ?device= / ?devices= / ?server=')
assert.match(rulesPage, /\/discovery\/rules\/run/, 'the rule list links to Run on inventory')
assert.match(rulesPage, /canUndoRun\(r\)/, 'undo is offered only where the model allows it')
assert.match(rulesPage, /rulesApi\.conflicts/, 'the rule list has a Conflicts panel')
assert.match(read('src/pages/device/ConfigurationTab.tsx'), /useAssetAutomation\(isManagedChild\(device\) \? 'child' : 'device', device\.id\)/,
  'the device Configuration tab has the Automation card (a controller-managed child is explained as a child)')
assert.match(read('src/pages/servers/detail/tabs/AgentTab.tsx'), /useAssetAutomation\('server', server\.id\)/, 'the server settings have the Automation panel')
assert.match(read('src/pages/DevicesPage.tsx'), /\/discovery\/rules\/run\?devices=/, 'the device list bulk bar runs rules on the selection')
assert.match(read('src/pages/discovery/RuleEditorPage.tsx'), /Runs automatically on/)
assert.match(read('src/pages/discovery/RuleEditorPage.tsx'), /When a value was changed by hand/)
assert.match(read('src/pages/discovery/api.ts'), /triggers: draft\.triggers/, 'create / update send the triggers')
for (const file of ['RuleRunPage.tsx', 'AssetAutomation.tsx']) {
  assert(!/text-\[\d+px\]/.test(read(`src/pages/discovery/${file}`)), `${file} uses the rem type scale, never pixel font sizes`)
}

// ── phase 3: the "changed" trigger, on_unmatch, controller-managed children ──
assert.equal(d.on_unmatch, 'keep', 'a new rule keeps what it set when an asset stops matching')
assert.equal(M.TRIGGER_LABEL.changed, 'Devices and servers whenever they change (and when the rule changes)')
assert.equal(M.ruleRunsText({ triggers: ['created', 'changed'], enforcement: 'set_once' }), 'runs on new devices and servers and changes · set once')
assert.equal(M.ruleRunsText({ triggers: ['changed'], enforcement: 'enforce', on_unmatch: 'revert' }),
  'runs on changes · enforced · reverts when no longer matching')
assert.equal(M.ruleRunsText({ triggers: ['changed'], on_unmatch: 'keep' }), 'runs on changes · set once', 'keep is the quiet default')
assert.equal(M.ON_UNMATCH_LABEL.keep, 'Keep the values the rule set')
assert.match(M.ON_UNMATCH_LABEL.revert, /^Revert them to what they were/)
assert.match(M.ON_UNMATCH_HELP, /automatic re-checks.*manual runs/, 'the note says where on_unmatch applies')
assert.deepEqual(M.ASSET_KIND_ORDER, ['device', 'server', 'child'])
assert.equal(M.ASSET_KIND_LABEL.child, 'Controller-managed switches and access points')

const catalog3 = {
  ...catalog2,
  actions: [
    ...catalog2.actions.map((a) =>
      ['group', 'owner', 'tags'].includes(a.key) ? { ...a, asset_kinds: [...a.asset_kinds, 'child'] } : a),
    { key: 'location', label: 'Location', kind: 'single', value: 'text', permission: ['devices.manage'], asset_kinds: ['device', 'child'], source: null, help: '', min: null, max: null },
    { key: 'map', label: 'Add to network map', kind: 'additive', value: 'ref', permission: ['maps.manage'], asset_kinds: ['device', 'server', 'child'], source: 'manual_maps', help: '', min: null, max: null },
    { key: 'dependency_parent', label: 'Depends on (uplink parent)', kind: 'single', value: 'ref', permission: ['devices.manage'], asset_kinds: ['device'], source: 'devices', help: '', min: null, max: null },
  ],
}
const keys = (list) => list.map((a) => a.key).sort()
assert.deepEqual(keys(M.pickableActions(catalog3, ['child'])), ['group', 'location', 'map', 'owner', 'tags'],
  'a rule on children is offered only group, location, owner, tags and map')
assert.deepEqual(keys(M.pickableActions(catalog3, ['device', 'child'])), ['group', 'location', 'map', 'owner', 'tags'],
  'a rule that includes children is offered only what applies to them')
assert(keys(M.pickableActions(catalog3, ['device'])).includes('dependency_parent'), 'devices are offered the uplink parent')
assert(!keys(M.pickableActions(catalog3, ['server'])).includes('dependency_parent'), 'servers are not offered the uplink parent')
assert.deepEqual(keys(M.pickableActions(catalog3, ['child'], ['tags', 'map'])), ['group', 'location', 'owner'], 'actions already on the rule are left out')
assert.deepEqual(M.actionProblems({ actions: [{ key: 'template', value: 'x' }], asset_kinds: ['child'] },
  { ...catalog3, actions: [...catalog3.actions, { key: 'template', label: 'Monitoring template', kind: 'single', value: 'ref', permission: ['devices.manage'], asset_kinds: ['device'], source: 'device_profiles', help: '', min: null, max: null }] }),
  ['Monitoring template applies to network devices only'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'decision', value: 'review' }], asset_kinds: ['child'] }, catalog3),
  ['Import decision applies to network devices and servers only'])
assert.equal(M.kindsPhrase(['device', 'server', 'child']), 'network devices, servers and controller-managed children')

// Field hints count children only for a rule that includes them.
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server', 'child'] }, ['child']), '')
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server'] }, ['device', 'child']), 'not children')
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server'] }, ['device']), '', 'children do not matter to a device rule')
assert.equal(M.fieldSourceHint({ applies_to: ['child'] }, ['child']), 'children only')
assert.equal(M.fieldSourceHint({ applies_to: ['child'] }), 'children only', 'a children-only field says so even without kinds')
assert.equal(M.fieldSourceHint({ applies_to: ['device', 'child'] }, ['child']), 'devices and children')
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server', 'child'] }), '', 'known everywhere')

// Activation permissions: a map placement is a map edit; children need devices.manage.
assert.deepEqual(M.activationPermissions([{ key: 'map', value: 'm' }], ['server'], catalog3), ['maps.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'map', value: 'm' }], ['child'], catalog3), ['maps.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'tags', value: ['a'] }], ['child'], catalog3), ['devices.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'dependency_parent', value: 'd' }], ['device'], catalog3), ['devices.manage'])

// Maps and uplink parents: names, links, added like tags.
const MAP1 = 'dddddddd-0000-0000-0000-000000000001'
const DEV1 = 'eeeeeeee-0000-0000-0000-000000000001'
const lookups3 = { ...lookups, manual_maps: { [MAP1]: 'Core network' }, devices: { [DEV1]: 'core-sw-01' } }
assert.equal(M.summarizeAction({ key: 'map', value: MAP1 }, lookups3), 'Map: Core network')
assert.equal(M.summarizeAction({ key: 'dependency_parent', value: DEV1 }, lookups3), 'Depends on: core-sw-01')
assert.equal(M.summarizeAction({ key: 'dependency_parent', value: DEV1 }), 'Depends on: eeeeeeee…', 'an unknown device keeps a short id')
assert.equal(M.formatSettingValue('map', MAP1, lookups3), 'Core network')
assert.equal(M.formatSettingValue('dependency_parent', DEV1, lookups3), 'core-sw-01')
assert.equal(M.settingLabel('map', null), 'Network map')
assert.equal(M.settingLabel('dependency_parent', null), 'Depends on (uplink parent)')
assert.equal(M.settingValueLink('map', MAP1), `/maps/manual?map=${MAP1}`, 'a map opens in the maps editor')
assert.equal(M.settingValueLink('dependency_parent', DEV1), `/devices/${DEV1}`)
assert.equal(M.settingValueLink('group', 'x'), null)
assert.equal(M.settingValueLink('map', null), null)
assert.equal(M.changeText('map', null, MAP1, lookups3), '+ Core network', 'a map placement is only added')
assert.equal(M.changeText('dependency_parent', DEV1, null, lookups3), '− core-sw-01')
assert.deepEqual(M.dependencyIds([
  { key: 'dependency_parent', value: DEV1.toUpperCase() }, { key: 'map', value: MAP1 },
  { action_key: 'dependency_parent', item_key: DEV1, before: null, after: DEV1 },
]), [DEV1], 'uplink parents to name, de-duplicated and lower-cased')
assert.equal(M.assetPath('child', 'abc'), '/devices/abc', 'a controller-managed child has a device page')

// Reverts: preview wording, the changes table text, outcome chip and counts.
assert.equal(M.CHANGE_OUTCOME_LABEL.revert, 'Revert')
assert.equal(M.CHANGE_OUTCOME_TONE.revert, 'warning')
assert.deepEqual(M.CHANGE_OUTCOME_ORDER, ['apply', 'revert', 'kept_manual_edit', 'not_reversible'])
const G1 = 'aaaaaaaa-0000-0000-0000-000000000001'
assert.equal(M.proposedText({ action_key: 'group', item_key: '', before: null, after: G1, outcome: 'apply' }, lookups),
  'not set → Datacenter firewalls')
assert.equal(M.proposedText({ action_key: 'location', item_key: '', before: 'DC-2', after: 'DC-1', outcome: 'revert' }),
  'Revert to DC-1 (now DC-2)')
assert.equal(M.proposedText({ action_key: 'group', item_key: '', before: G1, after: null, outcome: 'revert' }, lookups),
  'Revert to not set (now Datacenter firewalls)')
assert.equal(M.proposedText({ action_key: 'tags', item_key: 'datacenter', before: 'datacenter', after: null, outcome: 'revert' }), 'Remove datacenter')
assert.equal(M.proposedText({ action_key: 'map', item_key: MAP1, before: null, after: null, outcome: 'revert' }, lookups3),
  'Remove Core network', 'a revert of a map placement names the map from the item key')
assert.equal(M.proposedText({ action_key: 'tags', item_key: 'dc', before: null, after: 'dc', outcome: 'apply' }), '+ dc')

assert.equal(M.summarizeInventoryPreview({ matched: 10, changes: 4, reverts: 2, assets_changed: 3 }, ['device']),
  '10 devices match; 4 values would change and 2 would revert on 3 assets')
assert.equal(M.summarizeInventoryPreview({ matched: 0, changes: 0, reverts: 1, assets_changed: 1 }, ['child']),
  '0 devices match; 1 value would revert on 1 asset', 'children are devices; a revert alone is still a change')
assert.equal(M.summarizeInventoryPreview({ matched: 3, changes: 0, reverts: 0 }, ['device', 'server', 'child']),
  '3 devices and servers match; nothing would change')
assert.equal(M.summarizeApplyResult({ applied: 5, reverted: 2 }), 'Changed 5 values · reverted 2')
assert.equal(M.applyConfirmText({ changes: 17, reverts: 2, assets_changed: 9 }), '17 values will change and 2 will be reverted on 9 devices and servers.')
assert.equal(M.applyConfirmText({ changes: 0, reverts: 1, assets_changed: 1 }), '1 value will be reverted on 1 device or server.')
assert.equal(M.applyConfirmText({ changes: 3, assets_changed: 2 }), '3 values will change on 2 devices and servers.')
assert.deepEqual(M.buildTargets({ kinds: ['child'], device_ids: [D1], child_ids: [D1] }), { kinds: ['child'], child_ids: [D1] })
assert.deepEqual(M.parseRunTargets(`?child=${D1}`), { device_ids: [], server_ids: [], child_ids: [D1], kinds: ['child'] },
  'the Automation card of a managed child pre-selects it')
assert.deepEqual(M.parseRunTargets(`?children=${D1},${S1}&device=${D2}`).kinds, ['device', 'child'])

// Approval of automatic re-checks.
assert.equal(M.runTriggerText({ trigger: 'changed', mode: 'apply', discovery_run_id: null }), 'Rules re-checked · apply')
assert.equal(M.TRIGGER_REASON.changed, 'Asset or rule changed')
assert.equal(M.runStatusLabel('needs_approval'), 'Waiting for approval')
assert.equal(M.runStatusLabel('rejected'), 'Rejected')
assert.equal(M.runStatusLabel('something_else'), 'something else')
assert.equal(M.runStatusTone('needs_approval'), 'warning')
assert.equal(M.runStatusTone('rejected'), 'outline')
assert.equal(M.pendingCount({ counts: { changes: 118, reverts: 2 } }), 120)
assert.equal(M.waitingText([{ status: 'completed', counts: { changes: 5 } }]), null, 'no banner without a waiting run')
assert.equal(M.waitingText([{ status: 'needs_approval', counts: { changes: 118, reverts: 2 } }, { status: 'completed', counts: {} }]),
  '120 changes wait for approval')
assert.equal(M.waitingText([{ status: 'needs_approval', counts: { changes: 1 } }]), '1 change waits for approval')
assert.equal(M.waitingText([{ status: 'needs_approval', counts: { changes: 101 } }, { status: 'needs_approval', counts: { reverts: 150 } }]),
  '251 changes wait for approval in 2 runs')
assert.equal(M.summarizeApprovalResult({ status: 'partial', applied: 118, reverted: 2, skipped_changed: 3 }),
  'Applied 118 values · reverted 2 · 3 skipped (changed since the plan)')
assert.match(M.summarizeApprovalResult({ status: 'rejected' }), /^Rejected: not proposed again until an asset or a rule changes/)
assert(!M.canUndoRun({ mode: 'apply', status: 'needs_approval', trigger: 'changed' }), 'nothing to undo before approval')
assert(!M.canUndoRun({ mode: 'apply', status: 'rejected', trigger: 'changed' }), 'a rejected run changed nothing')
assert(M.canUndoRun({ mode: 'apply', status: 'completed', trigger: 'changed' }), 'an approved or automatic re-check can be undone')

// Export / import.
assert.equal(M.exportFileName(new Date(2026, 10, 7, 23, 59)), 'discovery-rules-2026-11-07.json', 'local date, zero padded')
assert.deepEqual(M.parseRulesFile('{"format":"zenplus-discovery-rules","version":1,"rules":[]}'),
  { document: { format: 'zenplus-discovery-rules', version: 1, rules: [] } })
assert.deepEqual(M.parseRulesFile('not json'), { error: 'The file is not valid JSON.' })
assert.deepEqual(M.parseRulesFile('{"format":"other","rules":[]}'), { error: 'This is not a ZenPlus discovery rules export.' })
assert.deepEqual(M.parseRulesFile('[1,2]'), { error: 'This is not a ZenPlus discovery rules export.' })

// ── phase 3 wiring ───────────────────────────────────────────────────────────
const api3 = read('src/pages/discovery/api.ts')
assert.match(api3, /on_unmatch: draft\.on_unmatch/, 'create / update send on_unmatch')
for (const [fn, route] of [['pending', '/pending'], ['approve', '/approve'], ['reject', '/reject'], ['exportRules', '/export'], ['importRules', '/import'], ['starter', '/starter'], ['addStarter', '/starter']]) {
  assert(new RegExp(`async ${fn}\\(`).test(api3) && api3.includes(route), `rulesApi.${fn} calls ${route}`)
}
const rules3 = read('src/pages/discovery/RulesPage.tsx')
assert.match(rules3, /waitingText\(runs\)/, 'the Rules page shows a banner while changes wait for approval')
assert.match(rules3, /params\.get\('review'\)/, 'the review panel opens from ?review=<run id>')
assert.match(rules3, /canManage && waiting &&/, 'Approve / Reject only for rule managers, only while the run waits')
assert.match(rules3, /rulesApi\.approve\(runId\)/)
assert.match(rules3, /rulesApi\.reject\(runId\)/)
assert.match(rules3, /<ChangesTable /, 'the review panel uses the same changes table as Run on inventory')
assert.match(rules3, /new Blob\(/, 'export downloads a file built in the browser')
assert.match(rules3, /exportFileName\(/)
assert.match(rules3, /new FileReader\(\)/, 'import reads a local file')
assert.match(rules3, /rulesApi\.addStarter/, 'the Rules page offers starter rules')
assert.match(rules3, /max_changes_per_run/, 'the settings panel edits the approval limit')
assert.match(read('src/pages/discovery/RuleRunPage.tsx'), /<ChangesTable /)
const editor3 = read('src/pages/discovery/RuleEditorPage.tsx')
assert.match(editor3, /When an asset stops matching/)
assert.match(editor3, /max_changes_per_run/, 'the editor warns with the approval limit when the changed trigger is active')
assert.match(editor3, /pickableActions\(catalog, draft\.asset_kinds/, 'the action picker follows the asset kinds')
assert.match(editor3, /<DevicePicker /, 'Depends on picks a device by search')
assert.match(read('src/pages/discovery/ruleUi.tsx'), /api\.get\('\/maps'\)/, 'map choices come from the network maps list')
for (const file of ['RulesPage.tsx', 'RuleEditorPage.tsx', 'RuleRunPage.tsx', 'AssetAutomation.tsx', 'ruleUi.tsx', 'ruleModel.ts']) {
  assert(!/text-\[\d+px\]/.test(read(`src/pages/discovery/${file}`)), `${file} uses the rem type scale, never pixel font sizes`)
}

// ── phase 4: interfaces, sensors, agent policies, service checks ─────────────
assert.equal(d.max_changes_per_run, null, 'a new rule has no change limit of its own')
assert.equal(M.TRIGGER_LABEL.scheduled, 'All devices and servers at the scheduled times')
assert.equal(M.ruleRunsText({ triggers: ['changed', 'scheduled'] }), 'runs on changes and a schedule · set once')
assert.equal(M.runTriggerText({ trigger: 'scheduled', mode: 'apply', discovery_run_id: null }), 'Scheduled run · apply')
assert(M.TRIGGER_REASON.scheduled, 'a scheduled run says why it ran')
assert.match(M.ON_UNMATCH_HELP, /scheduled runs/)
assert.deepEqual(M.ruleProblems({ ...M.defaultRule(), name: 'x', max_changes_per_run: 0 }, catalog),
  ['Changes per automatic run: a whole number from 1 to 100000, or empty'])
assert.deepEqual(M.ruleProblems({ ...M.defaultRule(), name: 'x', max_changes_per_run: 250 }, catalog), [])

// Speed ranges in Mbit/s, as actions._speed_range reads them.
assert.deepEqual(M.parseSpeedRange('1000'), { min: 1000, max: 1000 })
assert.deepEqual(M.parseSpeedRange('100-1000'), { min: 100, max: 1000 })
assert.deepEqual(M.parseSpeedRange(' 10000- '), { min: 10000, max: null })
assert.deepEqual(M.parseSpeedRange('-100'), { min: null, max: 100 })
for (const bad of ['', '-', 'fast', '1000-100', '1g', '10-20-30']) assert.equal(M.parseSpeedRange(bad), null, `"${bad}" is not a speed range`)

const P = (set, field, pattern) => ({ set, field, pattern })
assert.deepEqual(M.defaultInterfacePattern(), P('monitor', 'if_name', ''))
assert.deepEqual(M.interfacePatternProblems([P('monitor', 'if_name', '^gi'), P('ignore', 'if_type', '24, 131'), P('ignore', 'speed', '-100')]), [])
assert.deepEqual(M.interfacePatternProblems([]), ['add at least one pattern'])
assert.deepEqual(M.interfacePatternProblems([P('monitor', 'if_name', '  ')]), ['pattern 1: a pattern is required'])
assert.deepEqual(M.interfacePatternProblems([P('watch', 'if_name', 'x')]), ['pattern 1: monitor or ignore'])
assert.deepEqual(M.interfacePatternProblems([P('ignore', 'if_mtu', 'x')]), ['pattern 1: choose what it tests'])
assert.deepEqual(M.interfacePatternProblems([P('ignore', 'if_type', 'ethernet')]), ['pattern 1: IF-MIB type numbers separated by commas (6, 24, 131)'])
assert.deepEqual(M.interfacePatternProblems([P('ignore', 'speed', '1000-100')]), ['pattern 1: Mbit/s as 1000, 100-1000, 10000- or -100'])
assert.deepEqual(M.interfacePatternProblems([P('ignore', 'if_alias', 'x'.repeat(201))]), ['pattern 1: at most 200 characters'])
assert.deepEqual(M.interfacePatternProblems(Array.from({ length: 11 }, () => P('monitor', 'if_name', 'x'))), ['at most 10 patterns'])
assert.deepEqual(M.interfacePatternProblems(Array.from({ length: 3 }, () => P('monitor', 'if_name', 'x')), 2), ['at most 2 patterns'],
  'the catalog limit wins')

assert.equal(M.interfacePatternText(P('monitor', 'if_name', '^gi')), 'monitor where name matches ^gi')
assert.equal(M.interfacePatternText(P('ignore', 'if_type', '24,131')), 'ignore where type is 24 or 131')
assert.equal(M.interfacePatternText(P('ignore', 'speed', '-100')), 'ignore where speed is at most 100 Mbit/s')
assert.equal(M.interfacePatternText(P('monitor', 'speed', '10000-')), 'monitor where speed is at least 10000 Mbit/s')
assert.equal(M.interfacePatternText(P('monitor', 'speed', '100-1000')), 'monitor where speed is 100–1000 Mbit/s')
assert.equal(M.interfacePatternText(P('monitor', 'speed', '1000')), 'monitor where speed is 1000 Mbit/s')
assert.equal(M.summarizeAction({ key: 'interfaces', value: [P('monitor', 'if_alias', 'uplink'), P('ignore', 'if_type', '24')] }),
  'Interfaces: monitor where alias matches uplink; ignore where type is 24', 'patterns are summarised in test order')

const catalog4 = {
  ...catalog3,
  limits: { ...catalog3.limits, max_interface_patterns: 10 },
  actions: [
    ...catalog3.actions,
    { key: 'interfaces', label: 'Monitor or ignore interfaces', kind: 'additive', value: 'interfaces', permission: ['devices.manage'], asset_kinds: ['device'], source: null, help: '', min: null, max: null },
    { key: 'sensor', label: 'Monitor from sensor', kind: 'additive', value: 'ref', permission: ['devices.manage'], asset_kinds: ['device'], source: 'sensors', help: '', min: null, max: null },
    { key: 'agent_policy', label: 'Agent policy', kind: 'single', value: 'ref', permission: ['servers.manage'], asset_kinds: ['server'], source: 'agent_policies', help: '', min: null, max: null },
    { key: 'service_template', label: 'Add service check from template', kind: 'additive', value: 'ref', permission: ['service_checks.manage'], asset_kinds: ['device'], source: 'service_check_templates', help: '', min: null, max: null },
  ],
}
assert.deepEqual(M.defaultActionValue({ key: 'interfaces', value: 'interfaces' }), { key: 'interfaces', value: [P('monitor', 'if_name', '')] })
assert.deepEqual(M.actionProblems({ actions: [{ key: 'interfaces', value: [P('monitor', 'if_name', '')] }], asset_kinds: ['device'] }, catalog4),
  ['Monitor or ignore interfaces: pattern 1: a pattern is required'])
assert.deepEqual(M.actionProblems({ actions: [{ key: 'interfaces', value: [P('ignore', 'if_type', '24')] }], asset_kinds: ['server'] }, catalog4),
  ['Monitor or ignore interfaces applies to network devices only'])
assert(!keys(M.pickableActions(catalog4, ['server'])).includes('interfaces'), 'servers are not offered interface patterns')
assert(!keys(M.pickableActions(catalog4, ['server'])).includes('sensor'), 'servers are not offered a sensor')
assert(keys(M.pickableActions(catalog4, ['server'])).includes('agent_policy'), 'servers are offered an agent policy')
assert(!keys(M.pickableActions(catalog4, ['device'])).includes('agent_policy'), 'devices are not offered an agent policy')
assert.deepEqual(keys(M.pickableActions(catalog4, ['child'])), ['group', 'location', 'map', 'owner', 'tags'], 'children get none of the phase 4 actions')
assert.deepEqual(M.activationPermissions([{ key: 'service_template', value: 't' }], ['server'], catalog4), ['service_checks.manage'],
  'a check from a template is a service check edit, whatever the asset')
assert.deepEqual(M.activationPermissions([{ key: 'agent_policy', value: 'p' }], ['server'], catalog4), ['servers.manage'])
assert.deepEqual(M.activationPermissions([{ key: 'sensor', value: 's' }], ['device'], catalog4), ['devices.manage'])

const SEN1 = 'ffffffff-0000-0000-0000-000000000001'
const POL1 = 'ffffffff-0000-0000-0000-000000000002'
const TPL1 = 'ffffffff-0000-0000-0000-000000000003'
const lookups4 = { ...lookups3, sensors: { [SEN1]: 'Branch sensor' }, agent_policies: { [POL1]: 'Linux default' }, service_check_templates: { [TPL1]: 'HTTPS' } }
assert.equal(M.summarizeAction({ key: 'sensor', value: SEN1 }, lookups4), 'Monitor from sensor: Branch sensor')
assert.equal(M.summarizeAction({ key: 'agent_policy', value: POL1 }, lookups4), 'Agent policy: Linux default')
assert.equal(M.summarizeAction({ key: 'service_template', value: TPL1 }, lookups4), 'Service check: HTTPS')
assert.equal(M.summarizeAction({ key: 'sensor', value: SEN1 }), 'Monitor from sensor: ffffffff…', 'an unknown sensor keeps a short id')
assert.equal(M.formatSettingValue('agent_policy', POL1, lookups4), 'Linux default')
assert.equal(M.changeText('agent_policy', null, POL1, lookups4), 'not set → Linux default')
assert.equal(M.changeText('sensor', null, SEN1, lookups4), '+ Branch sensor', 'a sensor is only added, like a map')
assert.equal(M.changeText('service_template', null, TPL1, lookups4), '+ HTTPS')
for (const k of ['interfaces', 'sensor', 'agent_policy', 'service_template']) assert(M.settingLabel(k, null), `${k} has a label without the catalog`)

// Interfaces: item_key is the lower-cased ifName or "#<ifIndex>"; before / after mean monitored.
assert.equal(M.interfaceLabel('gi1/0/2'), 'Interface gi1/0/2')
assert.equal(M.interfaceLabel('#12'), 'Interface ifIndex 12')
assert.equal(M.changeText('interfaces', true, false, undefined, 'gi1/0/2'), 'Interface gi1/0/2: monitored → ignored')
assert.equal(M.proposedText({ action_key: 'interfaces', item_key: '#7', before: false, after: true, outcome: 'apply' }), 'Interface ifIndex 7: ignored → monitored')
assert.equal(M.proposedText({ action_key: 'interfaces', item_key: 'te1/1', before: false, after: true, outcome: 'revert' }),
  'Interface te1/1: revert to monitored (now ignored)')
assert.equal(M.formatSettingValue('interfaces', true), 'monitored')
assert.equal(M.itemOutcomeLabel('not_ready', 'sensor'), 'Waiting: sensor not online')
assert.equal(M.itemOutcomeLabel('not_ready', null), 'Not ready', 'a scan result that is not ready keeps its label')
assert.equal(M.itemOutcomeLabel('applied', 'sensor'), 'Applied')
assert.match(M.summarizeApplyResult({ applied: 3, not_ready: 2 }), /2 waiting for a sensor to come online/)

// ── phase 4: schedules ───────────────────────────────────────────────────────
assert.deepEqual(M.SCHEDULE_HOURS, [1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168], 'the same intervals as reconcile.SCHEDULE_HOURS')
assert.equal(M.scheduleEveryText(1), 'Every hour')
assert.equal(M.scheduleEveryText(6), 'Every 6 hours')
assert.equal(M.scheduleEveryText(24), 'Daily')
assert.equal(M.scheduleEveryText(72), 'Every 3 days')
assert.equal(M.scheduleEveryText(168), 'Weekly (Mondays)')
assert.equal(M.scheduleText({ schedule_every_hours: 6, schedule_time: '02:30', schedule_timezone: 'Asia/Riyadh' }), 'Every 6 hours from 02:30 (Asia/Riyadh)')
assert.equal(M.scheduleText({ schedule_every_hours: 24, schedule_time: '02:00', schedule_timezone: 'UTC' }), 'Daily at 02:00 (UTC)')
assert.equal(M.scheduleText({ schedule_every_hours: 48, schedule_time: '23:15', schedule_timezone: 'UTC' }), 'Every 2 days at 23:15 (UTC)')
assert.equal(M.scheduleText({ schedule_every_hours: 168, schedule_time: '01:00', schedule_timezone: 'Europe/Berlin' }), 'Weekly on Mondays at 01:00 (Europe/Berlin)')
assert.equal(M.scheduleText({}), 'Daily at 02:00 (UTC)', 'the API defaults')
for (const ok of ['00:00', '09:05', '23:59']) assert(M.isScheduleTime(ok), ok)
for (const bad of ['24:00', '9:05', '12:60', '', null]) assert(!M.isScheduleTime(bad), String(bad))
assert(M.isTimeZone('UTC') && M.isTimeZone('Asia/Riyadh'))
assert(!M.isTimeZone('Mars/Olympus') && !M.isTimeZone('') && !M.isTimeZone(null))
assert(M.isTimeZone(M.browserTimeZone()), 'the browser time zone is a valid default')
const sched = { growth_breaker_pct: 50, growth_breaker_min: 20, schedule_enabled: true, schedule_every_hours: 24, schedule_time: '02:00', schedule_timezone: 'UTC' }
assert.deepEqual(M.engineSettingsProblems(sched), [])
assert.deepEqual(M.engineSettingsProblems({ ...sched, schedule_every_hours: 5 }), ['Schedule: choose how often'])
assert.deepEqual(M.engineSettingsProblems({ ...sched, schedule_time: '2am' }), ['Schedule: a time as HH:MM'])
assert.deepEqual(M.engineSettingsProblems({ ...sched, schedule_timezone: 'Nowhere/City' }), ['Schedule: a known time zone, such as Asia/Riyadh or UTC'])
assert.deepEqual(M.engineSettingsProblems({ ...sched, schedule_enabled: false, schedule_timezone: '' }), [], 'an unused schedule is not checked')
assert.deepEqual(M.engineSettingsProblems({ ...sched, growth_breaker_pct: -1 }), ['Growth stop: a percentage from 0 (off) to 10000'])
assert.deepEqual(M.engineSettingsProblems({ ...sched, growth_breaker_pct: 0 }), [], '0 turns the growth stop off')
const now = Date.parse('2026-11-17T10:00:00Z')
assert.equal(M.untilText('2026-11-17T10:25:00Z', now), 'in 25 min')
assert.equal(M.untilText('2026-11-17T13:20:00Z', now), 'in 3 h 20 min')
assert.equal(M.untilText('2026-11-18T10:00:00Z', now), 'in 24 h')
assert.equal(M.untilText('2026-11-24T10:00:00Z', now), 'in 7 days')
assert.equal(M.untilText('2026-11-17T09:00:00Z', now), 'now')
assert.equal(M.untilText(null, now), '')
assert.equal(M.scheduleRunText({ skipped: 'no_rules' }), 'No active or simulating rule has the scheduled trigger')
assert.equal(M.scheduleRunText({ skipped: 'something_new' }), 'Something new')
assert.equal(M.scheduleRunText({ needs_approval: 'r1', changes: 140, reasons: ['140 changes exceed the limit of 100 per automatic run'] }),
  '140 changes wait for approval: 140 changes exceed the limit of 100 per automatic run')
assert.equal(M.scheduleRunText({ assets: 300, matched: 40, applied: 12, not_ready: 1 }),
  'Checked 300 assets · matched 40 · applied 12 values · 1 waiting for a sensor to come online')

// ── phase 4: unmatched ───────────────────────────────────────────────────────
assert.equal(M.unmatchedKindsText({ kinds: { server: { assets: 40, unmatched: 3 }, device: { assets: 300, unmatched: 12 } } }),
  '12 of 300 network devices · 3 of 40 servers', 'kinds in the usual order')
assert.equal(M.unmatchedKindsText({ kinds: {} }), '')

// ── phase 4 wiring ───────────────────────────────────────────────────────────
const api4 = read('src/pages/discovery/api.ts')
assert.match(api4, /max_changes_per_run: draft\.max_changes_per_run/, 'create / update send the rule change limit')
for (const [fn, route] of [['schedule', '/schedule'], ['runScheduleNow', '/schedule/run-now'], ['unmatched', '/unmatched']]) {
  assert(new RegExp(`async ${fn}\\(`).test(api4) && api4.includes(route), `rulesApi.${fn} calls ${route}`)
}
const ui4 = read('src/pages/discovery/ruleUi.tsx')
for (const [source, url] of [['sensors', '/sensors'], ['agent_policies', '/agent-policies'], ['service_check_templates', '/service-check-templates']]) {
  assert(ui4.includes(`api.get('${url}')`) && ui4.includes(`${source}:`), `${source} choices come from ${url}`)
}
const rules4 = read('src/pages/discovery/RulesPage.tsx')
assert.match(rules4, /<UnmatchedCard \/>/, 'the Rules page has the Unmatched report')
assert.match(rules4, /Count simulating rules as matching/)
assert.match(rules4, /rulesApi\.runScheduleNow/, 'rule managers can run the scheduled rules now')
assert.match(rules4, /canManage && \(\s*<Button[\s\S]{0,900}Run scheduled rules now/, 'Run now only for rule managers')
assert.match(rules4, /browserTimeZone\(\)/, 'a new schedule starts in the browser time zone')
assert.match(rules4, /growth_breaker_pct/, 'the settings edit the growth stop')
assert.match(rules4, /data\?\.reasons/, 'the approval view lists why a run waits')
assert.match(rules4, /itemOutcomeLabel\(it\.outcome, it\.action_key\)/, 'run items label a waiting sensor')
const editor4 = read('src/pages/discovery/RuleEditorPage.tsx')
assert.match(editor4, /'scheduled'\]/, 'the editor keeps the scheduled trigger of a loaded rule')
assert.match(editor4, /<InterfacePatternsEditor/)
assert.match(editor4, /Changes per automatic run, at most/)
assert.match(editor4, /first matching pattern decides/, 'the editor explains how interface patterns combine')
assert.match(read('src/pages/discovery/AssetAutomation.tsx'), /interfaceLabel\(s\.item_key\)/, 'the Automation card names the interface')

// ── server inventory conditions (services, software, watched processes) ──────
// Operators as conditions.OPERATORS["inventory"]; fields as server_inventory_facts.FIELDS.
const invOps = [op('has', 'has any of', 'list'), op('has_containing', 'has one containing', 'text'), op('has_none', 'has none of', 'list'),
  op('has_none_containing', 'has none containing', 'text'), op('reported', 'is reported', 'none'), op('not_reported', 'is not reported', 'none')]
const invField = (key, label) => ({ key, type: 'inventory', label, help: '', values: null, source: null, applies_to: ['server'], operators: invOps })
const invCatalog = {
  ...catalog,
  fields: [...catalog.fields, invField('services', 'Windows services'), invField('running_services', 'Running Windows services'),
    invField('software', 'Installed software'), invField('software_publishers', 'Software publishers'),
    invField('watched_processes', 'Watched processes running')],
}
assert.equal(M.summarizeLeaf(L('running_services', 'has', ['MSSQLSERVER']), invCatalog), 'Running Windows services has any of MSSQLSERVER')
assert.equal(M.summarizeLeaf(L('services', 'has', ['MSSQLSERVER', 'W3SVC']), invCatalog), 'Windows services has any of MSSQLSERVER or W3SVC')
assert.equal(M.summarizeLeaf(L('software', 'has_containing', 'SQL Server'), invCatalog), 'Installed software has one containing SQL Server')
assert.equal(M.summarizeLeaf(L('software_publishers', 'has_none', ['Oracle Corporation']), invCatalog), 'Software publishers has none of Oracle Corporation')
assert.equal(M.summarizeLeaf(L('watched_processes', 'has_none_containing', 'java'), invCatalog), 'Watched processes running has none containing java')
assert.equal(M.summarizeLeaf(L('services', 'not_reported', null), invCatalog), 'Windows services is not reported')
assert.equal(M.summarizeLeaf(L('software', 'reported', null), invCatalog), 'Installed software is reported')
assert.equal(M.summarizeCondition({ op: 'all', conditions: [L('os', 'contains', 'Windows'), L('running_services', 'has', ['MSSQLSERVER'])] }, invCatalog),
  'os contains Windows and Running Windows services has any of MSSQLSERVER', 'an unknown field keeps its key; inventory reads as a sentence')
// value kinds: list operators start empty and need a value, text needs text, "reported" needs none
assert.deepEqual(M.changeLeafOperator(L('services', 'has', ['A']), 'has_none', invCatalog), L('services', 'has_none', ['A']), 'list -> list keeps the names')
assert.deepEqual(M.changeLeafOperator(L('services', 'has', ['A']), 'has_containing', invCatalog), L('services', 'has_containing', ''))
assert.deepEqual(M.changeLeafOperator(L('services', 'has', ['A']), 'not_reported', invCatalog), L('services', 'not_reported', null))
assert.deepEqual(M.changeLeafField(L('services', 'has', ['MSSQLSERVER']), 'running_services', invCatalog), L('running_services', 'has', ['MSSQLSERVER']),
  'switching between inventory fields keeps the test and the names')
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('services', 'has', [])] }, invCatalog), ['Windows services has any of: give at least one value'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('software', 'has_containing', ' ')] }, invCatalog), ['Installed software has one containing: a value is required'])
assert.deepEqual(M.conditionProblems({ op: 'all', conditions: [L('services', 'not_reported', null)] }, invCatalog), [])
assert.equal(M.fieldSourceHint(invField('services', 'Windows services')), 'servers only')
// the builder hint: case-insensitive, exact vs part of a name, and what "not reported" means
assert.equal(M.isInventoryField(invField('software', 'Installed software')), true)
assert.equal(M.isInventoryField(catalog.fields[0]), false)
assert.equal(M.inventoryHint(catalog.fields[0], 'equals'), '', 'no hint for other fields')
const hHas = M.inventoryHint(invField('running_services', 'Running Windows services'), 'has')
assert.match(hHas, /exact service name or display name, ignoring case/)
assert.match(hHas, /older Linux agents no services/)
assert.match(hHas, /Linux servers no software list/)
assert.match(hHas, /servers without an agent report nothing/)
assert.match(hHas, /every other test, “has none” included, is then false/)
assert.match(M.inventoryHint(invField('software', 'Installed software'), 'has_none'), /exact software name/)
assert.match(M.inventoryHint(invField('software', 'Installed software'), 'has_containing'), /part of a software name, ignoring case/)
assert.doesNotMatch(M.inventoryHint(invField('services', 'Windows services'), 'not_reported'), /Matches/, '"is reported" tests need no name')
assert.match(M.inventoryHint(invField('services', 'Windows services'), 'reported'), /not refreshed it for 24 hours/, 'a stale list is not reported')
assert.equal(M.inventoryPlaceholder('services', 'list'), 'MSSQLSERVER, W3SVC')
assert.equal(M.inventoryPlaceholder('software', 'text'), 'SQL Server')
assert.equal(M.inventoryPlaceholder('unknown', 'list'), 'Comma separated')
assert.equal(M.fieldSourceHint({ applies_to: ['scan', 'device', 'server'] }), '', 'device group fields carry no source hint')
assert.match(read('src/pages/groups/api.ts'), /applies_to: f\.applies_to \?\? \['scan', 'device', 'server'\]/, 'the groups catalog never says "scan only"')
const editorInv = read('src/pages/discovery/RuleEditorPage.tsx')
assert.match(editorInv, /field\?\.type === 'inventory' && <div[^>]*>\{inventoryHint\(field, leaf\.operator\)\}/, 'the builder shows the inventory hint')
assert.match(editorInv, /inventoryPlaceholder\(field\.key, 'list'\)/)
assert.match(editorInv, /inventoryPlaceholder\(field\.key, 'text'\)/)

console.log('discovery rules model: ok')
