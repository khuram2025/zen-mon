// Discovery rules: the pure model behind the rule builder, the rule list and the scan-result columns.
// No React and no API calls here, so scripts/test-discovery-rules.mjs can load it in node.
//
// A condition is a tree of groups ({op, conditions}) and leaves ({field, operator, value}); a path is the list of
// child indexes from the root group ([] is the root, [1, 0] the first child of the root's second child).

import type {
  AssetKind,
  CatalogAction,
  CatalogField,
  ChangeOutcome,
  EngineRun,
  Enforcement,
  FieldSource,
  InventoryApplyResult,
  InventorySummary,
  InventoryTargets,
  RuleTrigger,
  UndoResult,
  CatalogOperator,
  ConditionGroup,
  ConditionLeaf,
  ConditionNode,
  Disposition,
  EngineRunOutcome,
  GroupOp,
  OperatorValueKind,
  PreviewSummary,
  RuleAction,
  RuleCatalog,
  RuleDraft,
  RuleMode,
} from './types'

export type Path = number[]

/** source name ("device_groups", ...) -> id -> display name */
export type Lookups = Record<string, Record<string, string>>

export type Tone = 'success' | 'info' | 'warning' | 'danger' | 'outline' | 'default'

export const DEFAULT_LIMITS = { max_depth: 3, max_leaves: 50, max_regex: 200 }

// ── defaults ────────────────────────────────────────────────────────────────

export function defaultRule(): RuleDraft {
  return {
    name: '',
    description: null,
    mode: 'draft',
    asset_kinds: ['device'],
    profile_ids: null,
    condition: { op: 'all', conditions: [] },
    actions: [{ key: 'decision', value: 'review' }],
    stop_processing: false,
    triggers: ['discovery'],
    enforcement: 'set_once',
  }
}

export function defaultValueFor(kind: OperatorValueKind): any {
  switch (kind) {
    case 'none': return null
    case 'list':
    case 'cidr_list':
    case 'ref_list': return []
    case 'number': return 0
    case 'range': return [0, 0]
    default: return ''
  }
}

export function fieldOf(catalog: RuleCatalog | null | undefined, key: string): CatalogField | undefined {
  return catalog?.fields.find((f) => f.key === key)
}

export function operatorOf(field: CatalogField | undefined, key: string): CatalogOperator | undefined {
  return field?.operators.find((o) => o.key === key)
}

/** A new leaf on the given field (default: the catalog's first field), first operator, empty value. */
export function defaultLeaf(catalog: RuleCatalog, fieldKey?: string): ConditionLeaf {
  const field = (fieldKey && fieldOf(catalog, fieldKey)) || catalog.fields[0]
  const op = field?.operators[0]
  return { field: field?.key ?? '', operator: op?.key ?? '', value: defaultValueFor(op?.value ?? 'text') }
}

/** Switch a leaf to another field: keep the operator when the new field has it, keep the value when its kind is unchanged. */
export function changeLeafField(leaf: ConditionLeaf, fieldKey: string, catalog: RuleCatalog): ConditionLeaf {
  const field = fieldOf(catalog, fieldKey)
  if (!field) return leaf
  const oldOp = operatorOf(fieldOf(catalog, leaf.field), leaf.operator)
  const sameOp = operatorOf(field, leaf.operator)
  const op = sameOp ?? field.operators[0]
  const sameKind = !!oldOp && !!op && oldOp.value === op.value && fieldOf(catalog, leaf.field)?.type === field.type
  return { field: field.key, operator: op?.key ?? '', value: sameKind ? leaf.value : defaultValueFor(op?.value ?? 'text') }
}

export function changeLeafOperator(leaf: ConditionLeaf, opKey: string, catalog: RuleCatalog): ConditionLeaf {
  const field = fieldOf(catalog, leaf.field)
  const oldOp = operatorOf(field, leaf.operator)
  const op = operatorOf(field, opKey)
  if (!op) return leaf
  return { ...leaf, operator: op.key, value: oldOp && oldOp.value === op.value ? leaf.value : defaultValueFor(op.value) }
}

// ── tree edits (immutable) ──────────────────────────────────────────────────

export function isGroup(node: ConditionNode | null | undefined): node is ConditionGroup {
  return !!node && typeof node === 'object' && 'op' in node
}

export function getNode(root: ConditionGroup, path: Path): ConditionNode | undefined {
  let node: ConditionNode | undefined = root
  for (const i of path) {
    if (!isGroup(node)) return undefined
    node = node.conditions[i]
  }
  return node
}

/** Replace the node at `path` with fn(node). */
export function updateNode(root: ConditionGroup, path: Path, fn: (node: ConditionNode) => ConditionNode): ConditionGroup {
  if (path.length === 0) {
    const next = fn(root)
    return isGroup(next) ? next : root
  }
  const [head, ...rest] = path
  const child = root.conditions[head]
  if (child === undefined) return root
  const replaced = rest.length === 0 ? fn(child) : isGroup(child) ? updateNode(child, rest, fn) : child
  return { ...root, conditions: root.conditions.map((c, i) => (i === head ? replaced : c)) }
}

export function setGroupOp(root: ConditionGroup, path: Path, op: GroupOp): ConditionGroup {
  return updateNode(root, path, (n) => (isGroup(n) ? { ...n, op } : n))
}

export function countLeaves(node: ConditionNode): number {
  return isGroup(node) ? node.conditions.reduce((n, c) => n + countLeaves(c), 0) : 1
}

/** Nesting depth of the tree: a lone root group is 1. */
export function treeDepth(node: ConditionNode): number {
  return isGroup(node) ? 1 + Math.max(0, ...node.conditions.map((c) => (isGroup(c) ? treeDepth(c) : 0))) : 0
}

/** A leaf can be added anywhere while the tree is under the leaf limit. */
export function canAddLeaf(root: ConditionGroup, limits = DEFAULT_LIMITS): boolean {
  return countLeaves(root) < limits.max_leaves
}

/** A sub-group can be added inside the group at `path` while it stays within max_depth (the root is level 1). */
export function canAddGroup(root: ConditionGroup, path: Path, limits = DEFAULT_LIMITS): boolean {
  return isGroup(getNode(root, path)) && path.length + 2 <= limits.max_depth && canAddLeaf(root, limits)
}

export function addLeaf(root: ConditionGroup, path: Path, leaf: ConditionLeaf, limits = DEFAULT_LIMITS): ConditionGroup {
  if (!canAddLeaf(root, limits)) return root
  return updateNode(root, path, (n) => (isGroup(n) ? { ...n, conditions: [...n.conditions, leaf] } : n))
}

/** Add a sub-group holding one starter leaf (an empty `any` group would match nothing). */
export function addGroup(root: ConditionGroup, path: Path, op: GroupOp, leaf: ConditionLeaf, limits = DEFAULT_LIMITS): ConditionGroup {
  if (!canAddGroup(root, path, limits)) return root
  return updateNode(root, path, (n) => (isGroup(n) ? { ...n, conditions: [...n.conditions, { op, conditions: [leaf] }] } : n))
}

export function removeNode(root: ConditionGroup, path: Path): ConditionGroup {
  if (path.length === 0) return { ...root, conditions: [] }
  const parent = path.slice(0, -1)
  const idx = path[path.length - 1]
  return updateNode(root, parent, (n) => (isGroup(n) ? { ...n, conditions: n.conditions.filter((_, i) => i !== idx) } : n))
}

// ── values ──────────────────────────────────────────────────────────────────

/** Comma / newline separated list -> trimmed, non-empty, de-duplicated strings. */
export function parseList(text: string): string[] {
  const out: string[] = []
  for (const part of String(text ?? '').split(/[\n,]+/)) {
    const v = part.trim()
    if (v && !out.includes(v)) out.push(v)
  }
  return out
}

export function listText(value: unknown): string {
  return Array.isArray(value) ? value.join(', ') : String(value ?? '')
}

function refName(lookups: Lookups | undefined, source: string | null | undefined, id: unknown): string {
  const s = String(id ?? '')
  return (source && lookups?.[source]?.[s]) || (s.length > 12 ? `${s.slice(0, 8)}…` : s)
}

function joinWords(items: string[], word: 'or' | 'and'): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} ${word} ${items[items.length - 1]}`
}

/** Strip the builder hint from an operator label ("is under (OID subtree)" -> "is under"). */
export function plainOperator(label: string): string {
  return label.replace(/\s*\([^)]*\)\s*$/, '')
}

export function formatLeafValue(leaf: ConditionLeaf, catalog: RuleCatalog | null | undefined, lookups?: Lookups): string {
  const field = fieldOf(catalog, leaf.field)
  const op = operatorOf(field, leaf.operator)
  const kind = op?.value ?? 'text'
  const v = leaf.value
  switch (kind) {
    case 'none': return ''
    case 'range': {
      const [lo, hi] = Array.isArray(v) ? v : [v, v]
      return `${lo ?? '?'} and ${hi ?? '?'}`
    }
    case 'ref': return refName(lookups, field?.source, v)
    case 'ref_list': return joinWords((Array.isArray(v) ? v : []).map((id) => refName(lookups, field?.source, id)), 'or')
    case 'list':
    case 'cidr_list': {
      const items = (Array.isArray(v) ? v : parseList(String(v ?? ''))).map(String)
      return joinWords(items, leaf.operator === 'has_all' ? 'and' : 'or')
    }
    default: return String(v ?? '')
  }
}

export function summarizeLeaf(leaf: ConditionLeaf, catalog: RuleCatalog | null | undefined, lookups?: Lookups): string {
  const field = fieldOf(catalog, leaf.field)
  const op = operatorOf(field, leaf.operator)
  const value = formatLeafValue(leaf, catalog, lookups)
  const parts = [field?.label ?? leaf.field, op ? plainOperator(op.label) : leaf.operator]
  if (value !== '') parts.push(value)
  return parts.join(' ')
}

/** One English sentence for a condition tree. */
export function summarizeCondition(node: ConditionNode, catalog: RuleCatalog | null | undefined, lookups?: Lookups): string {
  return summarize(node, catalog, lookups, true)
}

function summarize(node: ConditionNode, catalog: RuleCatalog | null | undefined, lookups: Lookups | undefined, top: boolean): string {
  if (!isGroup(node)) return summarizeLeaf(node, catalog, lookups)
  const kids = node.conditions
  if (kids.length === 0) {
    if (node.op === 'any') return 'matches nothing'
    return top ? 'every discovered asset' : 'always true'
  }
  const parts = kids.map((c) => {
    const s = summarize(c, catalog, lookups, false)
    return isGroup(c) && c.conditions.length > 1 && c.op !== 'none' ? `(${s})` : s
  })
  if (node.op === 'none') return `not (${parts.join(' or ')})`
  return parts.join(node.op === 'all' ? ' and ' : ' or ')
}

/** Capitalise a sentence unless it starts with a mixed-case name ("sysObjectID ..." stays as it is). */
export function sentenceCase(s: string): string {
  const first = s.split(/\s/, 1)[0] ?? ''
  return first && first === first.toLowerCase() ? s[0].toUpperCase() + s.slice(1) : s
}

/** Client-side checks before save / preview; the server validates again. */
export function conditionProblems(root: ConditionGroup, catalog: RuleCatalog | null | undefined, limits = DEFAULT_LIMITS): string[] {
  const out: string[] = []
  if (countLeaves(root) > limits.max_leaves) out.push(`A rule holds at most ${limits.max_leaves} conditions`)
  if (treeDepth(root) > limits.max_depth) out.push(`Groups nest at most ${limits.max_depth} levels`)
  const walk = (n: ConditionNode) => {
    if (isGroup(n)) { n.conditions.forEach(walk); return }
    const field = fieldOf(catalog, n.field)
    const op = operatorOf(field, n.operator)
    if (!field || !op) { out.push(`Choose a field and a test for every condition`); return }
    const label = `${field.label} ${plainOperator(op.label)}`
    const v = n.value
    switch (op.value) {
      case 'none': break
      case 'list':
      case 'cidr_list':
      case 'ref_list':
        if (!Array.isArray(v) || v.length === 0) out.push(`${label}: give at least one value`)
        break
      case 'number':
        if (typeof v !== 'number' || Number.isNaN(v)) out.push(`${label}: a number is required`)
        break
      case 'range':
        if (!Array.isArray(v) || v.length !== 2 || v.some((x) => typeof x !== 'number' || Number.isNaN(x))) out.push(`${label}: two numbers are required`)
        else if (v[0] > v[1]) out.push(`${label}: the first number must not exceed the second`)
        break
      default:
        if (String(v ?? '').trim() === '') out.push(`${label}: a value is required`)
        if (n.operator === 'matches' && String(v ?? '').length > limits.max_regex) out.push(`${label}: at most ${limits.max_regex} characters`)
    }
  }
  walk(root)
  return Array.from(new Set(out))
}

// ── actions ─────────────────────────────────────────────────────────────────

export const DECISION_LABEL: Record<string, string> = {
  auto_import: 'Auto-import',
  review: 'Keep for review',
  ignore: 'Ignore',
}

export const IMPORT_AS_LABEL: Record<string, string> = {
  auto: 'auto-routed',
  device: 'network device',
  server: 'server',
  both: 'server and device',
}

const ACTION_SOURCE: Record<string, string> = {
  group: 'device_groups',
  template: 'device_profiles',
  snmp_credential: 'snmp_credentials',
}

export function summarizeAction(action: RuleAction, lookups?: Lookups, catalog?: RuleCatalog | null): string {
  const v = action.value
  switch (action.key) {
    case 'decision':
      if (v === 'auto_import') return `Auto-import as ${IMPORT_AS_LABEL[action.import_as || 'device'] ?? action.import_as}`
      return DECISION_LABEL[v] ?? String(v)
    case 'group': return `Group: ${refName(lookups, ACTION_SOURCE.group, v)}`
    case 'template': return `Template: ${refName(lookups, ACTION_SOURCE.template, v)}`
    case 'snmp_credential': return `SNMP credential: ${refName(lookups, ACTION_SOURCE.snmp_credential, v)}`
    case 'tags': return `Tags: ${(Array.isArray(v) ? v : parseList(String(v ?? ''))).join(', ')}`
    case 'ping_enabled': return 'Turn ping on'
    case 'owner': return `Owner: ${v}`
    case 'environment': return `Environment: ${v}`
    case 'ping_interval': return `Ping every ${v} s`
    case 'snmp_interval': return `SNMP poll every ${v} s`
    case 'device_type': return `Type: ${v}`
    case 'location': return `Location: ${v}`
    default: {
      const spec = catalog?.actions.find((a) => a.key === action.key)
      const shown = Array.isArray(v) ? v.join(', ') : spec?.source ? refName(lookups, spec.source, v) : String(v ?? '')
      return `${spec?.label ?? action.key}: ${shown}`
    }
  }
}

/** One chip text per action, the decision first. */
export function summarizeActions(actions: RuleAction[], lookups?: Lookups, catalog?: RuleCatalog | null): string[] {
  const sorted = [...(actions || [])].sort((a, b) => (a.key === 'decision' ? -1 : b.key === 'decision' ? 1 : 0))
  return sorted.map((a) => summarizeAction(a, lookups, catalog))
}

export function decisionTone(value: string | undefined): Tone {
  return value === 'auto_import' ? 'success' : value === 'ignore' ? 'outline' : value === 'review' ? 'info' : 'default'
}

/** Empty draft value for an action key (the decision defaults to review). */
export function defaultActionValue(spec: { key: string; value: string; min?: number | null }): RuleAction {
  switch (spec.value) {
    case 'decision': return { key: spec.key, value: 'review' }
    case 'tags': return { key: spec.key, value: [] }
    case 'on': return { key: spec.key, value: true }
    case 'int': return { key: spec.key, value: spec.key === 'snmp_interval' ? 300 : spec.min && spec.min > 60 ? spec.min : 60 }
    default: return { key: spec.key, value: '' }
  }
}

export function actionProblems(draft: Pick<RuleDraft, 'actions' | 'asset_kinds'>, catalog: RuleCatalog | null | undefined): string[] {
  const out: string[] = []
  if (!draft.actions.length) out.push('Add at least one action')
  const seen = new Set<string>()
  for (const a of draft.actions) {
    const spec = catalog?.actions.find((s) => s.key === a.key)
    const label = spec?.label ?? a.key
    if (seen.has(a.key)) out.push(`${label}: only once per rule`)
    seen.add(a.key)
    if (!spec) continue
    if (!spec.asset_kinds.some((k) => draft.asset_kinds.includes(k))) {
      out.push(`${label} applies to ${spec.asset_kinds.map((k) => (k === 'device' ? 'network devices' : 'servers')).join(' and ')} only`)
    }
    if (spec.value === 'tags' && (!Array.isArray(a.value) || a.value.length === 0)) out.push(`${label}: give at least one tag`)
    if ((spec.value === 'ref' || spec.value === 'text') && String(a.value ?? '').trim() === '') out.push(`${label}: choose a value`)
    if (spec.value === 'on' && a.value !== true) out.push(`${label}: a rule can only turn it on`)
    if (spec.value === 'int') {
      const n = Number(a.value)
      if (!Number.isInteger(n) || (spec.min != null && n < spec.min) || (spec.max != null && n > spec.max)) {
        out.push(`${label}: between ${spec.min} and ${spec.max}`)
      }
    }
  }
  return out
}

export function ruleProblems(draft: RuleDraft, catalog: RuleCatalog | null | undefined): string[] {
  const out: string[] = []
  if (!draft.name.trim()) out.push('Give the rule a name')
  if (!draft.asset_kinds.length) out.push('Choose network devices, servers or both')
  if (draft.triggers && !draft.triggers.length) out.push('Choose when the rule runs: on scan results, on new devices and servers, or both')
  return [...out, ...conditionProblems(draft.condition, catalog, catalog?.limits ?? DEFAULT_LIMITS), ...actionProblems(draft, catalog)]
}

// ── modes, dispositions, outcomes ───────────────────────────────────────────

export const MODE_LABEL: Record<RuleMode, string> = { draft: 'Draft', simulate: 'Simulating', active: 'Active', disabled: 'Disabled' }
export const MODE_TONE: Record<RuleMode, Tone> = { draft: 'warning', simulate: 'info', active: 'success', disabled: 'outline' }
/** One line per mode for the editor's mode select. */
export const MODE_HELP: Record<RuleMode, string> = {
  draft: 'Never runs by itself; preview works.',
  simulate: 'Runs on scans and new devices and records what it would do, but changes nothing.',
  active: 'Runs by itself and makes its changes.',
  disabled: 'Never runs.',
}
export const MODE_ORDER: RuleMode[] = ['draft', 'simulate', 'active', 'disabled']

export const DISPOSITION_LABEL: Record<Disposition, string> = {
  imported: 'Imported',
  ignored: 'Ignored',
  review: 'Kept for review',
  would_import: 'Would import',
  would_ignore: 'Would ignore',
  existing: 'Already monitored',
  no_match: 'No rule matched',
  auto_import: 'Auto-import',
  ignore: 'Ignore',
}

export const DISPOSITION_TONE: Record<Disposition, Tone> = {
  imported: 'success',
  auto_import: 'success',
  ignored: 'outline',
  ignore: 'outline',
  review: 'info',
  would_import: 'info',
  would_ignore: 'default',
  existing: 'outline',
  no_match: 'default',
}

export const REASON_TEXT: Record<string, string> = {
  license_capacity_reached: 'license capacity reached',
  cap: 'per-scan auto-import limit reached',
  not_ready: 'not ready to import',
  profile_review: 'the profile only lets rules annotate',
  profile_ignore_only: 'the profile only lets rules ignore',
  conflict: 'conflicts with existing inventory',
  error: 'the import failed',
}

export function dispositionLabel(d: string | null | undefined): string {
  if (!d) return 'Not evaluated'
  return (DISPOSITION_LABEL as Record<string, string>)[d] ?? d.replace(/_/g, ' ')
}

export function dispositionTone(d: string | null | undefined): Tone {
  return (d && (DISPOSITION_TONE as Record<string, Tone>)[d]) || 'default'
}

export function reasonText(reason: string | null | undefined): string | null {
  if (!reason) return null
  return REASON_TEXT[reason] ?? reason.replace(/_/g, ' ')
}

/** "Kept for review — license capacity reached" */
export function dispositionText(d: string | null | undefined, reason?: string | null): string {
  const r = reasonText(reason)
  return r ? `${dispositionLabel(d)} — ${r}` : dispositionLabel(d)
}

export const OUTCOME_LABEL: Record<EngineRunOutcome, string> = {
  applied: 'Applied',
  kept_manual_edit: 'Hand edit kept',
  not_reversible: 'Not reversible',
  skipped_changed: 'Skipped: changed since',
  reverted: 'Reverted',
  unchanged: 'Unchanged',
  simulated: 'Simulated',
  shadowed: 'Shadowed',
  blocked_license: 'Blocked: license',
  blocked_cap: 'Blocked: per-scan limit',
  not_ready: 'Not ready',
  error: 'Error',
}

export const OUTCOME_TONE: Record<EngineRunOutcome, Tone> = {
  applied: 'success',
  kept_manual_edit: 'info',
  not_reversible: 'warning',
  skipped_changed: 'warning',
  reverted: 'default',
  unchanged: 'outline',
  simulated: 'info',
  shadowed: 'default',
  blocked_license: 'warning',
  blocked_cap: 'warning',
  not_ready: 'outline',
  error: 'danger',
}

/** "matches 42 of 318: would import 17, ignore 3, review 22; license room 12" */
export function summarizePreview(summary: PreviewSummary, licenseRoom: number | null | undefined): string {
  const n = (k: keyof PreviewSummary) => Number(summary[k] ?? 0)
  const imports = n('auto_import') + n('would_import')
  const ignores = n('ignore') + n('would_ignore')
  const parts: string[] = []
  if (imports) parts.push(`would import ${imports}`)
  if (ignores) parts.push(`ignore ${ignores}`)
  if (n('review')) parts.push(`review ${n('review')}`)
  if (n('existing')) parts.push(`already monitored ${n('existing')}`)
  let s = `matches ${n('matched')} of ${n('evaluated')}`
  if (parts.length) s += `: ${parts.join(', ')}`
  if (licenseRoom !== undefined) s += `; license room ${licenseRoom === null ? 'unlimited' : licenseRoom}`
  return s
}

/** Counts of a rule re-evaluation, for a toast: "Evaluated 318 · matched 42 · imported 17 · ignored 3". */
export function summarizeCounts(c: Record<string, any>): string {
  const order: [string, string][] = [
    ['evaluated', 'evaluated'], ['matched', 'matched'], ['imported', 'imported'], ['ignored', 'ignored'],
    ['would_import', 'would import'], ['would_ignore', 'would ignore'], ['review', 'kept for review'],
    ['existing', 'already monitored'], ['blocked_license', 'blocked by license'], ['blocked_cap', 'over the per-scan limit'],
    ['errors', 'errors'],
  ]
  return order
    .filter(([k]) => k === 'evaluated' || Number(c[k] ?? 0) > 0)
    .map(([k, label]) => `${label} ${Number(c[k] ?? 0)}`)
    .join(' · ')
    .replace(/^./, (ch) => ch.toUpperCase())
}

/** The trace leaf [field, operator, ok] as text: "sysObjectID is under ✓". */
export function traceLeafText(leaf: [string, string, boolean], catalog: RuleCatalog | null | undefined): string {
  const field = fieldOf(catalog, leaf[0])
  const op = operatorOf(field, leaf[1])
  return `${field?.label ?? leaf[0]} ${op ? plainOperator(op.label) : leaf[1]} ${leaf[2] ? '✓' : '✗'}`
}

export const SKIP_TEXT: Record<string, string> = {
  asset_kind: 'not for this kind of asset',
  profile: 'limited to other discovery profiles',
  trigger: 'not run on this trigger',
}

/** Move the id at `index` by `delta` (-1 up, +1 down); returns a new list, or the same one when out of range. */
export function moveItem<T>(list: T[], index: number, delta: number): T[] {
  const to = index + delta
  if (index < 0 || index >= list.length || to < 0 || to >= list.length) return list
  const next = [...list]
  const [item] = next.splice(index, 1)
  next.splice(to, 0, item)
  return next
}

/** Import-mode wording shared by the wizard and the rule editor. */
export const IMPORT_MODE_TEXT: Record<string, { label: string; desc: string }> = {
  review: {
    label: 'Review first — rules only annotate results (recommended)',
    desc: 'Rules show what they would do; nothing is imported or ignored without you.',
  },
  auto_match: {
    label: 'Let rules auto-import and ignore',
    desc: 'Results a rule marks Auto-import are imported, results it marks Ignore are ignored; the rest wait for review.',
  },
  ignore_match: {
    label: 'Let rules ignore only',
    desc: 'Results a rule marks Ignore are ignored; nothing is imported automatically.',
  },
}

// ── phase 2: triggers, enforcement, field sources ───────────────────────────

export const TRIGGER_LABEL: Record<RuleTrigger, string> = {
  discovery: 'Discovery scan results',
  created: 'Devices and servers when they are added',
}
const TRIGGER_SHORT: Record<string, string> = { discovery: 'scans', created: 'new devices and servers' }

export const ENFORCEMENT_LABEL: Record<Enforcement, string> = { set_once: 'Set once', enforce: 'Enforce' }
export const ENFORCEMENT_HELP: Record<Enforcement, string> = {
  set_once: 'A value someone changed by hand after the rule set it is kept.',
  enforce: "The rule puts its value back when someone changes it by hand.",
}

/** "Runs on scans and new devices and servers · set once" (rule list line). */
export function ruleRunsText(rule: { triggers?: string[] | null; enforcement?: string | null }): string {
  const t = (rule.triggers?.length ? rule.triggers : ['discovery']).map((k) => TRIGGER_SHORT[k] ?? k)
  const enf = rule.enforcement === 'enforce' ? 'enforced' : 'set once'
  return `runs on ${joinWords(t, 'and')} · ${enf}`
}

const SOURCE_WORD: Record<FieldSource, string> = { scan: 'scans', device: 'devices', server: 'servers' }

/**
 * Where a condition field has a value, as a short hint for the field picker: "" when it is known everywhere,
 * "scan only", "devices only", "devices and servers". A catalog without applies_to (phase 1 API) means scans only.
 */
export function fieldSourceHint(field: Pick<CatalogField, 'applies_to'> | undefined): string {
  const src = field?.applies_to ?? ['scan']
  if (src.length === 0) return ''
  if (src.length >= 3) return ''
  if (src.length === 1) return src[0] === 'scan' ? 'scan only' : `${SOURCE_WORD[src[0]]} only`
  return joinWords(src.map((k) => SOURCE_WORD[k] ?? k), 'and')
}

/** The permissions activating (or running) a rule needs, as the API computes them (actions.permissions_needed). */
export function activationPermissions(actions: RuleAction[], assetKinds: AssetKind[], catalog: RuleCatalog | null | undefined): string[] {
  const kinds = new Set<AssetKind>(assetKinds.length ? assetKinds : ['device'])
  const needed = new Set<string>()
  for (const a of actions) {
    const spec = catalog?.actions.find((s) => s.key === a.key)
    if (!spec) continue
    if (spec.key === 'decision') {
      needed.add('discovery.run')
      if (a.import_as === 'server' || a.import_as === 'both' || a.import_as === 'auto') needed.add('servers.manage')
      continue
    }
    const applies = spec.asset_kinds.filter((k) => kinds.has(k))
    if (applies.includes('device')) spec.permission.filter((p) => p !== 'servers.manage').forEach((p) => needed.add(p))
    if (applies.includes('server')) needed.add('servers.manage')
  }
  return Array.from(needed).sort()
}

// ── phase 2: inventory runs ─────────────────────────────────────────────────

export const CHANGE_OUTCOME_LABEL: Record<ChangeOutcome, string> = {
  apply: 'Will change',
  kept_manual_edit: 'Hand edit kept',
  not_reversible: 'Left alone',
}
export const CHANGE_OUTCOME_TONE: Record<ChangeOutcome, Tone> = {
  apply: 'success',
  kept_manual_edit: 'info',
  not_reversible: 'warning',
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function kindNoun(kinds: AssetKind[] | undefined, n: number): string {
  const k = new Set(kinds?.length ? kinds : ['device', 'server'])
  if (k.has('device') && !k.has('server')) return n === 1 ? 'device' : 'devices'
  if (k.has('server') && !k.has('device')) return n === 1 ? 'server' : 'servers'
  return n === 1 ? 'device or server' : 'devices and servers'
}

/** "42 devices and servers match; 17 values would change on 9 assets; 3 hand edits kept" */
export function summarizeInventoryPreview(summary: Partial<InventorySummary>, kinds?: AssetKind[]): string {
  const matched = Number(summary.matched ?? 0)
  const changes = Number(summary.changes ?? 0)
  const assets = Number(summary.assets_changed ?? 0)
  const kept = Number(summary.kept_manual_edit ?? 0)
  const notRev = Number(summary.not_reversible ?? 0)
  const parts = [`${matched} ${kindNoun(kinds, matched)} ${matched === 1 ? 'matches' : 'match'}`]
  parts.push(changes ? `${plural(changes, 'value')} would change on ${plural(assets, 'asset')}` : 'nothing would change')
  if (kept) parts.push(`${plural(kept, 'hand edit')} kept`)
  if (notRev) parts.push(`${notRev} left alone (not reversible)`)
  return parts.join('; ')
}

/** "Changed 17 values · 3 hand edits kept · 1 skipped (changed since the preview)" */
export function summarizeApplyResult(r: Partial<InventoryApplyResult>): string {
  const parts = [`Changed ${plural(Number(r.applied ?? 0), 'value')}`]
  if (r.kept_manual_edit) parts.push(`${plural(r.kept_manual_edit, 'hand edit')} kept`)
  if (r.not_reversible) parts.push(`${r.not_reversible} left alone (not reversible)`)
  if (r.skipped_changed) parts.push(`${r.skipped_changed} skipped (changed since the preview)`)
  return parts.join(' · ')
}

/** "Reverted 17 values · 2 left as they are (changed since)" */
export function summarizeUndoResult(r: Partial<UndoResult>): string {
  const parts = [`Reverted ${plural(Number(r.reverted ?? 0), 'value')}`]
  if (r.skipped_changed) parts.push(`${r.skipped_changed} left as they are (changed since)`)
  if (r.not_reversible) parts.push(`${r.not_reversible} not reversible`)
  const imported = r.imported_devices?.length ?? 0
  if (imported) parts.push(`${plural(imported, 'device')} imported by the scan`)
  return parts.join(' · ')
}

/** Undo is offered on a completed or partial run that applied changes, never on an undo, a simulation or an annotation. */
export function canUndoRun(run: Pick<EngineRun, 'mode' | 'status' | 'trigger'>): boolean {
  return run.mode === 'apply' && (run.status === 'completed' || run.status === 'partial') && run.trigger !== 'undo'
}

/** History "Trigger" column: "Scan · apply", "Run on inventory · apply", "Undo", "New asset · simulate". */
export function runTriggerText(run: Pick<EngineRun, 'trigger' | 'mode' | 'discovery_run_id'>): string {
  const what =
    run.trigger === 'discovery' ? 'Scan'
      : run.trigger === 'created' ? 'New device / server'
        : run.trigger === 'undo' ? 'Undo'
          : run.discovery_run_id ? 'Re-evaluated' : 'Run on inventory'
  if (run.trigger === 'undo') return what
  const how = run.mode === 'apply' ? 'apply' : run.mode === 'simulate' ? 'simulate' : 'annotate only'
  return `${what} · ${how}`
}

export function runStatusTone(status: string): Tone {
  return status === 'completed' ? 'success' : status === 'partial' ? 'warning' : status === 'failed' ? 'danger' : 'outline'
}

export function assetPath(kind: string | null | undefined, id: string | null | undefined): string | null {
  if (!id) return null
  if (kind === 'device') return `/devices/${id}`
  if (kind === 'server') return `/servers/${id}`
  return null
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function idList(...values: (string | null)[]): string[] {
  const out: string[] = []
  for (const v of values) {
    for (const part of String(v ?? '').split(',')) {
      const id = part.trim().toLowerCase()
      if (UUID_RE.test(id) && !out.includes(id)) out.push(id)
    }
  }
  return out
}

/**
 * Pre-selected assets from the run page's address: ?device=<id>, ?devices=<id,id>, ?server=<id>, ?servers=<id,id>.
 * Anything that is not an id is dropped. `kinds` follows what was pre-selected.
 */
export function parseRunTargets(search: string | URLSearchParams): { device_ids: string[]; server_ids: string[]; kinds: AssetKind[] } {
  const p = typeof search === 'string' ? new URLSearchParams(search) : search
  const device_ids = idList(p.get('device'), p.get('devices'))
  const server_ids = idList(p.get('server'), p.get('servers'))
  const kinds: AssetKind[] = []
  if (device_ids.length || !server_ids.length) kinds.push('device')
  if (server_ids.length || !device_ids.length) kinds.push('server')
  return { device_ids, server_ids, kinds }
}

/** An IPv4 or IPv6 network in CIDR form ("10.0.0.0/16", "fd00::/8"); the server checks it again. */
export function isSubnet(text: string): boolean {
  const s = text.trim()
  const m = /^([^/]+)\/(\d{1,3})$/.exec(s)
  if (!m) return false
  const bits = Number(m[2])
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(m[1])) {
    return bits <= 32 && m[1].split('.').every((o) => Number(o) <= 255)
  }
  return m[1].includes(':') && /^[0-9a-f:.]+$/i.test(m[1]) && bits <= 128
}

/** The request body's targets: empty filters left out, a pre-selection limited to the kinds that are ticked. */
export function buildTargets(f: {
  kinds: AssetKind[]
  device_ids?: string[]
  server_ids?: string[]
  group_ids?: string[]
  tags?: string[]
  subnet?: string
  query?: string
}): InventoryTargets {
  const t: InventoryTargets = { kinds: f.kinds }
  if (f.device_ids?.length && f.kinds.includes('device')) t.device_ids = f.device_ids
  if (f.server_ids?.length && f.kinds.includes('server')) t.server_ids = f.server_ids
  if (f.group_ids?.length) t.group_ids = f.group_ids
  if (f.tags?.length) t.tags = f.tags
  if (f.subnet?.trim()) t.subnet = f.subnet.trim()
  if (f.query?.trim()) t.query = f.query.trim()
  return t
}

/** A setting's display name: the catalog's action label ("Device group"), "Tag" for one tag. */
export function settingLabel(actionKey: string | null | undefined, catalog: RuleCatalog | null | undefined): string {
  if (!actionKey) return '—'
  if (actionKey === 'tags') return 'Tag'
  if (actionKey === 'ping_enabled') return 'Ping monitoring'
  const spec: CatalogAction | undefined = catalog?.actions.find((a) => a.key === actionKey)
  return spec?.label ?? SETTING_FALLBACK[actionKey] ?? sentenceCase(actionKey.replace(/_/g, ' '))
}

/** Labels for a page that cannot read the catalog (devices.view without discovery.view). */
const SETTING_FALLBACK: Record<string, string> = {
  decision: 'Import decision',
  group: 'Device group',
  template: 'Monitoring template',
  snmp_credential: 'SNMP credential',
  ping_interval: 'Ping interval',
  snmp_interval: 'SNMP poll interval',
  device_type: 'Device type',
  location: 'Location',
  owner: 'Owner',
  environment: 'Environment',
}

/** A setting value for people: group / template / credential names, On / Off, "not set". */
export function formatSettingValue(actionKey: string | null | undefined, value: any, lookups?: Lookups): string {
  if (value === null || value === undefined || value === '') return 'not set'
  if (typeof value === 'boolean') return value ? 'On' : 'Off'
  if (Array.isArray(value)) return value.length ? value.map(String).join(', ') : 'none'
  if (typeof value === 'object') {
    if ('value' in value) return formatSettingValue(actionKey, (value as any).value, lookups)
    return Object.entries(value).map(([k, x]) => `${k}: ${Array.isArray(x) ? x.join(', ') : String(x)}`).join(', ')
  }
  const source = actionKey ? ACTION_SOURCE[actionKey] : undefined
  if (source) return refName(lookups, source, value)
  if (actionKey === 'ping_interval' || actionKey === 'snmp_interval') return `${value} s`
  return String(value)
}

/** "Before → after" for a change or a history item (tags show "+ tag"). */
export function changeText(actionKey: string | null | undefined, before: any, after: any, lookups?: Lookups): string {
  if (actionKey === 'tags') {
    if (before == null && after != null) return `+ ${formatSettingValue('tags', after, lookups)}`
    if (after == null && before != null) return `− ${formatSettingValue('tags', before, lookups)}`
  }
  return `${formatSettingValue(actionKey, before, lookups)} → ${formatSettingValue(actionKey, after, lookups)}`
}

/** "Datacenter's Device group is overridden by Fortinet firewalls on 12 assets" */
export function overriddenText(
  o: { rule_name: string | null; winner_rule_name: string | null; action_key: string; assets: number },
  catalog: RuleCatalog | null | undefined,
): string {
  return `${o.rule_name ?? 'A rule'}: ${settingLabel(o.action_key, catalog).toLowerCase()} is overridden by ${o.winner_rule_name ?? 'an earlier rule'} on ${plural(o.assets, 'asset')}`
}
