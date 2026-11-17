// Discovery rules (phase 1, migrate-188; phase 2 on inventory, migrate-191; phase 3 re-checks, children, approval,
// export / import and starter rules, migrate-193): the pure model behind the rule builder, the rule list and the scan-result
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
  ['Choose when the rule runs: on scan results, on new devices and servers, or whenever they change'])
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

console.log('discovery rules model: ok')
