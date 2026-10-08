// Template designer model (features/reports/designer/model.ts): entry
// reordering, option defaults / summaries / validation, tags, drafts from
// built-in and organisation templates, payloads and library search.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const toUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
async function load(path) {
  const src = fs.readFileSync(path, 'utf8')
  const out = await transformWithEsbuild(src, path.replace(/\.tsx?$/, '.ts'), { loader: 'ts', format: 'esm' })
  assert.ok(!/from ['"]@\//.test(out.code), `${path} must not import app modules at runtime`)
  return import(toUrl(out.code))
}

const m = await load('src/features/reports/designer/model.ts')

let seq = 0
const uid = () => `u${++seq}`

/* ── Section definitions used below ────────────────────────────────── */
const topN = { key: 'top_n', label: 'Rows', type: 'int', default: 10, min: 1, max: 200, help: 'How many rows to list.' }
const latency = {
  id: 'device_latency_loss', title: 'Top Nodes by Latency', description: 'Round-trip time and packet loss per node.',
  category: 'Performance', scope: 'device', requires: ['devices.view'], semantics: 'window', available: true,
  options: [
    { key: 'metric', label: 'Rank by', type: 'select', default: 'rtt', choices: [['rtt', 'RTT'], ['loss', 'Packet loss']] },
    topN,
    { key: 'loss_threshold', label: 'Packet loss threshold (%)', type: 'number', default: 1.0, min: 0.01, max: 100 },
  ],
}
const flapping = {
  id: 'device_flapping', title: 'Unstable Devices (Flapping)', description: 'Devices that changed state often.',
  category: 'Availability', scope: 'device', requires: ['devices.view'], semantics: 'window', available: true,
  options: [
    { ...topN, default: 15 },
    { key: 'threshold', label: 'Flapping threshold (state changes in the period)', type: 'int', default: 4, min: 1, max: 1000 },
    { key: 'monitored_only', label: 'Monitored interfaces only', type: 'bool', default: false },
    { key: 'include_disabled', label: 'Include disabled', type: 'bool', default: true },
  ],
}
const flows = {
  id: 'traffic_talkers', title: 'Top Talkers', description: 'Hosts sending and receiving the most NetFlow traffic.',
  category: 'Traffic', scope: 'netflow', requires: ['netflow.view'], semantics: 'window', available: false, options: [],
}
const status = {
  id: 'device_status_now', title: 'Current Device Status', description: 'Status of every device right now.',
  category: 'Availability', scope: 'device', requires: [], semantics: 'snapshot', available: true, options: [],
}
const sections = { [latency.id]: latency, [flapping.id]: flapping, [flows.id]: flows, [status.id]: status }

/* ── Reordering ─────────────────────────────────────────────────────── */
const abc = ['a', 'b', 'c', 'd']
assert.deepEqual(m.moveEntry(abc, 0, 2), ['b', 'c', 'a', 'd'])
assert.deepEqual(m.moveEntry(abc, 3, 0), ['d', 'a', 'b', 'c'])
assert.deepEqual(m.moveEntry(abc, 1, 1), abc)
assert.deepEqual(m.moveEntry(abc, 1, 99), ['a', 'c', 'd', 'b'], 'target clamps to the end')
assert.deepEqual(m.moveEntry(abc, 2, -5), ['c', 'a', 'b', 'd'], 'target clamps to the start')
assert.deepEqual(m.moveEntry(abc, 7, 0), abc, 'unknown source is a no-op')
assert.notEqual(m.moveEntry(abc, 0, 1), abc, 'returns a new array')
assert.deepEqual(abc, ['a', 'b', 'c', 'd'], 'input is not mutated')

// Insertion slots: 0 = before the first item, length = after the last.
assert.equal(m.dropIndex(0, 0), 0)
assert.equal(m.dropIndex(0, 1), 0, 'dropping right after itself is a no-op')
assert.equal(m.dropIndex(0, 4), 3, 'after the last item')
assert.equal(m.dropIndex(3, 0), 0)
assert.equal(m.dropIndex(2, 2), 2)
assert.equal(m.dropIndex(2, 3), 2)
assert.deepEqual(m.moveEntry(abc, 1, m.dropIndex(1, 4)), ['a', 'c', 'd', 'b'])
assert.deepEqual(m.moveEntry(abc, 3, m.dropIndex(3, 1)), ['a', 'd', 'b', 'c'])

assert.deepEqual(m.insertEntry(['a', 'b'], 'x'), ['a', 'b', 'x'])
assert.deepEqual(m.insertEntry(['a', 'b'], 'x', 0), ['x', 'a', 'b'])
assert.deepEqual(m.insertEntry(['a', 'b'], 'x', 9), ['a', 'b', 'x'])
assert.deepEqual(m.removeEntry(['a', 'b', 'c'], 1), ['a', 'c'])
assert.deepEqual(m.removeEntry(['a'], 3), ['a'])

const entries = [
  { uid: 'e1', id: latency.id, title: '', options: { top_n: 5 } },
  { uid: 'e2', id: flapping.id, title: 'Flappers', options: {} },
]
const dup = m.duplicateEntry(entries, 0, 'e3')
assert.deepEqual(dup.map((e) => e.uid), ['e1', 'e3', 'e2'], 'the copy goes right after the original')
assert.deepEqual(dup[1].options, { top_n: 5 })
dup[1].options.top_n = 50
assert.equal(entries[0].options.top_n, 5, 'the copy has its own options')

assert.equal(m.addPosition(entries, 'e1'), 1, 'new sections go after the selected one')
assert.equal(m.addPosition(entries, null), 2, 'else at the end')
assert.equal(m.addPosition(entries, 'missing'), 2)
assert.equal(m.positionText('Top talkers', 2, 8), 'Top talkers, position 3 of 8')
assert.equal(m.entryTitle(entries[1], flapping), 'Flappers')
assert.equal(m.entryTitle(entries[0], latency), 'Top Nodes by Latency')
assert.equal(m.entryTitle({ id: 'gone', title: ' ' }), 'gone')
assert.deepEqual(m.usageCounts([{ id: 'a' }, { id: 'b' }, { id: 'a' }]), { a: 2, b: 1 })

// The n-th entry with a section id maps to the n-th rendered section with it.
const order = [{ id: 'x' }, { id: 'y' }, { id: 'x' }]
const rendered = [{ id: 'x', section_id: 'x' }, { id: 'x', section_id: 'x' }]
assert.equal(m.previewIndexFor(order, 0, rendered), 0)
assert.equal(m.previewIndexFor(order, 2, rendered), 1)
assert.equal(m.previewIndexFor(order, 1, rendered), -1, 'an omitted section has no preview')
assert.equal(m.previewIndexFor(order, 5, rendered), -1)

/* ── Options: defaults, sparse payloads, validation ────────────────── */
assert.deepEqual(m.defaultOptions(latency), { metric: 'rtt', top_n: 10, loss_threshold: 1 })
assert.deepEqual(m.effectiveOptions(latency, { top_n: 25, bogus: 1 }), { metric: 'rtt', top_n: 25, loss_threshold: 1 })
assert.deepEqual(m.sparseOptions(latency, { metric: 'rtt', top_n: 25, loss_threshold: '1' }), { top_n: 25 },
  'defaults and numeric strings equal to the default are dropped')
assert.deepEqual(m.sparseOptions(latency, { top_n: '30' }), { top_n: 30 }, 'numeric strings become numbers')
assert.deepEqual(m.sparseOptions(latency, { unknown_key: 3 }), {}, 'unknown keys are dropped')
assert.equal(m.isCustomized(latency, { top_n: 10 }), false)
assert.equal(m.isCustomized(latency, { metric: 'loss' }), true)

assert.equal(m.optionError(topN, 10), null)
assert.equal(m.optionError(topN, 0), 'Use a value between 1 and 200')
assert.equal(m.optionError(topN, 201), 'Use a value between 1 and 200')
assert.equal(m.optionError(topN, 2.5), 'Use a whole number')
assert.equal(m.optionError(topN, ''), 'Enter a number')
assert.equal(m.optionError(topN, 'abc'), 'Enter a number')
assert.equal(m.optionError(topN, '12'), null, 'numeric text is accepted')
assert.equal(m.optionError(latency.options[2], 0.5), null, 'decimals are fine for number options')
assert.equal(m.optionError(latency.options[2], 0), 'Use a value between 0.01 and 100')
assert.equal(m.optionError(latency.options[0], 'jitter'), 'Choose one of the listed values')
assert.equal(m.optionError(latency.options[0], 'loss'), null)
assert.equal(m.optionError(flapping.options[2], 'yes'), 'Turn this on or off')
assert.deepEqual(m.optionErrors(latency, { top_n: 0, metric: 'loss' }), { top_n: 'Use a value between 1 and 200' })
assert.deepEqual(m.optionErrors(latency, {}), {}, 'defaults are valid')

/* ── Option summaries ───────────────────────────────────────────────── */
assert.equal(m.optionSummary(latency, {}), 'Top 10 · Rank by RTT · Packet loss threshold 1%', 'top_n first, units folded in')
assert.equal(m.optionSummary(latency, { metric: 'loss', top_n: 5, loss_threshold: 2.5 }), 'Top 5 · Rank by Packet loss · Packet loss threshold 2.5%')
assert.equal(m.optionSummary(latency, {}, 2), 'Top 10 · Rank by RTT · +1 more')
assert.equal(m.optionSummary(flapping, {}), 'Top 15 · Flapping threshold 4 · Include disabled',
  'long parentheticals are dropped; false flags say nothing')
assert.equal(m.optionSummary(flapping, { monitored_only: true, include_disabled: false }, 5),
  'Top 15 · Flapping threshold 4 · Monitored interfaces only · Include disabled: off')
assert.equal(m.optionSummary(flows, {}), '', 'no options, no summary')
assert.equal(m.optionSummary(undefined, { top_n: 3 }), '')

/* ── Steppers ───────────────────────────────────────────────────────── */
assert.equal(m.stepValue(topN, 10, 1), 11)
assert.equal(m.stepValue(topN, 1, -1), 1, 'clamped at the minimum')
assert.equal(m.stepValue(topN, 200, 1), 200, 'clamped at the maximum')
assert.equal(m.stepValue(topN, 'oops', 1), 11, 'invalid input steps from the default')
assert.equal(m.stepValue(latency.options[2], 1, 1), 2)
assert.equal(m.stepValue(latency.options[2], 0.5, -1), 0.01, 'never below the minimum')
const ratio = { key: 'ratio', label: 'Ratio', type: 'number', default: 0.5, min: 0, max: 1 }
assert.equal(m.optionStep(ratio), 0.1, 'narrow ranges step by tenths')
assert.equal(m.stepValue(ratio, 0.5, 1), 0.6)
assert.equal(m.stepValue(ratio, 0.95, 1), 1)

/* ── Tags ───────────────────────────────────────────────────────────── */
assert.deepEqual(m.addTags([], 'weekly'), { tags: ['weekly'], rest: '', error: null })
assert.deepEqual(m.addTags(['sla'], ' weekly ,  noc  team , '), { tags: ['sla', 'weekly', 'noc team'], rest: '', error: null })
assert.deepEqual(m.addTags(['SLA'], 'sla'), { tags: ['SLA'], rest: '', error: '"sla" is already a tag' })
const long = 'x'.repeat(33)
assert.deepEqual(m.addTags([], `ok, ${long}`), { tags: ['ok'], rest: long, error: 'Tags are at most 32 characters' })
const twelve = Array.from({ length: 12 }, (_, i) => `t${i}`)
assert.deepEqual(m.addTags(twelve, 'extra'), { tags: twelve, rest: 'extra', error: 'Use at most 12 tags' })
assert.deepEqual(m.cleanTags([' a ', 'A', '', 'b', 7, 'y'.repeat(40)]), ['a', 'b'])
assert.equal(m.cleanTags(Array.from({ length: 20 }, (_, i) => `t${i}`)).length, 12)

/* ── Drafts ─────────────────────────────────────────────────────────── */
const blank = m.emptyDraft()
assert.equal(blank.name, '')
assert.equal(blank.visibility, 'private')
assert.equal(blank.defaultPeriod, 'last_7d')
assert.deepEqual(blank.entries, [])

const builtin = {
  key: 'network_health', title: 'Network Health', description: 'Weekly network review.', category: 'performance',
  engine: 'sections', formats: ['pdf'], sections: [latency.id, flapping.id],
  entries: [{ id: latency.id, options: { top_n: 20 } }, { id: flapping.id, title: 'Flapping nodes' }],
  tags: ['weekly', 'Weekly', 'network'], default_period: 'previous_week',
}
seq = 0
const fromBuiltin = m.draftFromBuiltin(builtin, uid)
assert.equal(fromBuiltin.name, 'Network Health (custom)')
assert.equal(fromBuiltin.description, 'Weekly network review.')
assert.equal(fromBuiltin.category, 'performance')
assert.equal(fromBuiltin.defaultPeriod, 'previous_week')
assert.equal(fromBuiltin.sourceKey, 'network_health')
assert.equal(fromBuiltin.visibility, 'private')
assert.deepEqual(fromBuiltin.tags, ['weekly', 'network'])
assert.deepEqual(fromBuiltin.entries, [
  { uid: 'u1', id: latency.id, title: '', options: { top_n: 20 } },
  { uid: 'u2', id: flapping.id, title: 'Flapping nodes', options: {} },
])
assert.deepEqual(m.entriesFrom([], ['a', 'b'], uid).map((e) => e.id), ['a', 'b'], 'older templates list bare ids')
assert.equal(m.draftFromBuiltin({ ...builtin, title: 'T'.repeat(130) }, uid).name.length, 120)

const custom = {
  id: '6d1c', name: 'Ops weekly', description: 'For the ops review', sections: [latency.id], entries: [{ id: latency.id, title: 'Slow nodes', options: { metric: 'loss' } }],
  category: 'performance', tags: ['ops'], visibility: 'shared', source_key: 'network_health', default_period: null,
  owner_id: 'u', owner_name: 'Jane', is_owner: false, can_edit: true, can_delete: true, can_change_visibility: false,
  schedules: 2, created_at: null, updated_at: null,
}
const edit = m.draftFromCustom(custom, 'edit', uid)
assert.equal(edit.name, 'Ops weekly')
assert.equal(edit.visibility, 'shared')
assert.equal(edit.defaultPeriod, null)
assert.equal(edit.sourceKey, 'network_health')
assert.equal(edit.entries[0].title, 'Slow nodes')
const clone = m.draftFromCustom(custom, 'clone', uid)
assert.equal(clone.name, 'Ops weekly (copy)')
assert.equal(clone.visibility, 'private', 'copies start private')

/* ── Validation ─────────────────────────────────────────────────────── */
const known = { categories: ['performance', 'availability'], periods: ['last_24h', 'last_7d', 'previous_week'] }
let v = m.validateDraft(blank, sections, known)
assert.equal(v.valid, false)
assert.deepEqual(v.issues.map((i) => i.key), ['field:name', 'field:sections'])
assert.equal(v.fields.name, 'Give the template a name')
assert.equal(v.fields.sections, 'Add at least one section')

v = m.validateDraft(fromBuiltin, sections, known)
assert.equal(v.valid, true, JSON.stringify(v.issues))
assert.equal(v.optionIssueCount, 0)

const broken = {
  ...fromBuiltin,
  name: 'n'.repeat(121),
  description: 'd'.repeat(2001),
  category: 'gone',
  tags: ['ok', 'z'.repeat(33)],
  entries: [
    { uid: 'b1', id: latency.id, title: 'T'.repeat(121), options: { top_n: 0, metric: 'nope' } },
    { uid: 'b2', id: 'removed_section', title: '', options: {} },
  ],
}
v = m.validateDraft(broken, sections, known)
assert.equal(v.valid, false)
assert.match(v.fields.name, /120 characters \(now 121\)/)
assert.match(v.fields.description, /2000 characters/)
assert.equal(v.fields.tags, 'Tags are 1–32 characters')
assert.equal(v.fields.category, 'This category no longer exists; choose another')
assert.deepEqual(Object.keys(v.entries.b1.options).sort(), ['metric', 'top_n'])
assert.equal(v.entries.b1.title, 'Keep the title to 120 characters')
assert.match(v.entries.b2.unknown, /no longer available/)
assert.equal(v.optionIssueCount, 1)
const optionIssue = v.issues.find((i) => i.key === 'entry:b1:top_n')
assert.deepEqual(optionIssue.target, { kind: 'entry', uid: 'b1', option: 'top_n' })
assert.equal(optionIssue.message, 'Section 1 (' + 'T'.repeat(121) + '): Rows — use a value between 1 and 200')

const tooMany = { ...fromBuiltin, entries: Array.from({ length: 41 }, (_, i) => ({ uid: `x${i}`, id: status.id, title: '', options: {} })) }
v = m.validateDraft(tooMany, sections, known)
assert.equal(v.fields.sections, 'A template holds at most 40 sections; remove 1')
assert.equal(m.validateDraft({ ...fromBuiltin, tags: Array.from({ length: 13 }, (_, i) => `t${i}`) }, sections).fields.tags, 'Use at most 12 tags')
assert.equal(m.validateDraft({ ...fromBuiltin, defaultPeriod: 'fortnight' }, sections, known).fields.defaultPeriod, 'Choose a default period from the list')
assert.equal(m.validateDraft({ ...fromBuiltin, name: '   ' }, sections).fields.name, 'Give the template a name')

// A section the author's role cannot view blocks saving a new copy (e.g. a
// customised built-in), but editing in place may keep one the template holds.
const withFlows = { ...fromBuiltin, entries: [...fromBuiltin.entries, { uid: 'f1', id: flows.id, title: '', options: {} }] }
v = m.validateDraft(withFlows, sections, known)
assert.equal(v.valid, false)
assert.match(v.entries.f1.restricted, /can't view this section/)
assert.equal(v.issues.find((i) => i.key === 'entry:f1:access').target.uid, 'f1')
v = m.validateDraft(withFlows, sections, { ...known, kept: new Set([flows.id]) })
assert.equal(v.valid, true, JSON.stringify(v.issues))
assert.equal(v.entries.f1, undefined)

/* ── Payloads and dirty tracking ────────────────────────────────────── */
const draft = {
  ...fromBuiltin,
  name: '  Network Health (custom) ',
  description: '  ',
  tags: ['weekly', 'Weekly'],
  entries: [
    { uid: 'p1', id: latency.id, title: '  Slow nodes ', options: { top_n: 10, metric: 'loss', stale: 1 } },
    { uid: 'p2', id: flapping.id, title: '', options: {} },
  ],
}
assert.deepEqual(m.toTemplateBody(draft, sections), {
  name: 'Network Health (custom)',
  description: null,
  sections: [{ id: latency.id, title: 'Slow nodes', options: { metric: 'loss' } }, { id: flapping.id }],
  category: 'performance',
  tags: ['weekly'],
  visibility: 'private',
  source_key: 'network_health',
  default_period: 'previous_week',
})
const sig = m.draftSignature(draft, sections)
assert.equal(m.draftSignature({ ...draft, entries: draft.entries.map((e) => ({ ...e, uid: `${e.uid}-new` })) }, sections), sig,
  'local ids do not make a draft dirty')
assert.equal(m.draftSignature({ ...draft, entries: [{ ...draft.entries[0], options: { metric: 'loss', top_n: 10 } }, draft.entries[1]] }, sections), sig,
  'setting an option back to its default is not a change')
assert.notEqual(m.draftSignature({ ...draft, entries: [...draft.entries].reverse() }, sections), sig, 'order matters')
assert.notEqual(m.draftSignature({ ...draft, visibility: 'shared' }, sections), sig)

/* ── Library search and grouping ────────────────────────────────────── */
const all = [latency, flapping, flows, status]
assert.deepEqual(m.groupSections(all).map((g) => [g.category, g.sections.map((s) => s.id)]), [
  ['Performance', [latency.id]],
  ['Availability', [flapping.id, status.id]],
  ['Traffic', [flows.id]],
])
assert.deepEqual(m.groupSections(all, 'netflow').flatMap((g) => g.sections.map((s) => s.id)), [flows.id], 'matches permissions and descriptions')
assert.deepEqual(m.groupSections(all, 'device status').flatMap((g) => g.sections.map((s) => s.id)), [status.id], 'every word must match')
assert.deepEqual(m.groupSections(all, 'LATENCY').flatMap((g) => g.sections.map((s) => s.id)), [latency.id], 'case-insensitive')
assert.deepEqual(m.groupSections(all, 'zzz'), [])

const categories = [
  { id: 'availability', title: 'Availability & Uptime', icon: 'Activity', description: '' },
  { id: 'alerts', title: 'Alerts & Events', icon: 'Bell', description: '' },
  { id: 'servers', title: 'Servers & Agents', icon: 'Server', description: '' },
  { id: 'traffic', title: 'Traffic Analysis', icon: 'Waypoints', description: '' },
]
assert.equal(m.catalogCategoryFor('Availability', categories)?.id, 'availability')
assert.equal(m.catalogCategoryFor('Traffic', categories)?.id, 'traffic')
assert.equal(m.catalogCategoryFor('Agents', categories)?.id, 'servers')
assert.equal(m.catalogCategoryFor('Events', categories)?.id, 'alerts')
assert.equal(m.catalogCategoryFor('Alerting', categories)?.id, 'alerts')
assert.equal(m.catalogCategoryFor('Assets', categories), undefined)

assert.equal(m.permissionLabel('netflow.view'), 'NetFlow (view)')
assert.equal(m.permissionLabel('service_checks.view'), 'Service Checks (view)')
assert.equal(m.permissionLabel('custom_mod.do_it'), 'custom mod (do it)')

console.log('report designer: all assertions passed')
