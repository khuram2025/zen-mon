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
  ApprovalResult,
  EngineRunStatus,
  InventoryApplyResult,
  InventoryChange,
  InventorySummary,
  OnUnmatch,
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
  InterfacePattern,
  RuleEngineSettings,
  ScheduleRunResult,
  UnmatchedReport,
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
    on_unmatch: 'keep',
    max_changes_per_run: null,
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
  map: 'manual_maps',
  dependency_parent: 'devices',
  sensor: 'sensors',
  agent_policy: 'agent_policies',
  service_template: 'service_check_templates',
}

/**
 * Actions whose value is one of a set (map placements, uplink parents, monitoring sensors, service checks made from
 * templates): only ever added, like tags.
 */
export const SET_ACTION_KEYS = new Set(['tags', 'map', 'dependency_parent', 'sensor', 'service_template'])

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
    case 'map': return `Map: ${refName(lookups, ACTION_SOURCE.map, v)}`
    case 'dependency_parent': return `Depends on: ${refName(lookups, ACTION_SOURCE.dependency_parent, v)}`
    case 'sensor': return `Monitor from sensor: ${refName(lookups, ACTION_SOURCE.sensor, v)}`
    case 'agent_policy': return `Agent policy: ${refName(lookups, ACTION_SOURCE.agent_policy, v)}`
    case 'service_template': return `Service check: ${refName(lookups, ACTION_SOURCE.service_template, v)}`
    case 'interfaces': return `Interfaces: ${summarizeInterfacePatterns(Array.isArray(v) ? v : [])}`
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
    case 'interfaces': return { key: spec.key, value: [defaultInterfacePattern()] }
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
      out.push(`${label} applies to ${kindsPhrase(spec.asset_kinds)} only`)
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
    if (spec.value === 'interfaces') {
      const max = catalog?.limits?.max_interface_patterns ?? MAX_INTERFACE_PATTERNS
      for (const p of interfacePatternProblems(Array.isArray(a.value) ? a.value : [], max)) out.push(`${label}: ${p}`)
    }
  }
  return out
}

export function ruleProblems(draft: RuleDraft, catalog: RuleCatalog | null | undefined): string[] {
  const out: string[] = []
  if (!draft.name.trim()) out.push('Give the rule a name')
  if (!draft.asset_kinds.length) out.push('Choose what the rule looks at: network devices, servers or controller-managed children')
  if (draft.triggers && !draft.triggers.length) out.push('Choose when the rule runs: on scan results, on new devices and servers, whenever they change, or at the scheduled times')
  const cap = draft.max_changes_per_run
  if (cap != null && (!Number.isInteger(cap) || cap < 1 || cap > 100000)) out.push('Changes per automatic run: a whole number from 1 to 100000, or empty')
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
  changed: 'Devices and servers whenever they change (and when the rule changes)',
  scheduled: 'All devices and servers at the scheduled times',
}
const TRIGGER_SHORT: Record<string, string> = {
  discovery: 'scans',
  created: 'new devices and servers',
  changed: 'changes',
  scheduled: 'a schedule',
}

/** When an asset stops matching (phase 3). */
export const ON_UNMATCH_LABEL: Record<OnUnmatch, string> = {
  keep: 'Keep the values the rule set',
  revert: 'Revert them to what they were (if unchanged since)',
}
export const ON_UNMATCH_HELP =
  'Applies to automatic re-checks (the “whenever they change” trigger), scheduled runs and manual runs on inventory.'

/** Asset kinds (phase 3 adds controller-managed FortiSwitches and FortiAPs). */
export const ASSET_KIND_ORDER: AssetKind[] = ['device', 'server', 'child']
export const ASSET_KIND_LABEL: Record<AssetKind, string> = {
  device: 'Network devices',
  server: 'Servers',
  child: 'Controller-managed switches and access points',
}
/** Short form for list lines ("Network devices + Managed switches / APs"). */
export const ASSET_KIND_SHORT: Record<AssetKind, string> = {
  device: 'Network devices',
  server: 'Servers',
  child: 'Managed switches / APs',
}
const KIND_PLURAL: Record<string, string> = { device: 'network devices', server: 'servers', child: 'controller-managed children' }

/** "network devices and servers", "controller-managed children" (an action's reach, in a sentence). */
export function kindsPhrase(kinds: string[]): string {
  return joinWords(kinds.map((k) => KIND_PLURAL[k] ?? k), 'and')
}

/**
 * Actions the editor offers for a rule on these kinds. A rule that includes controller-managed children is offered
 * only the actions that apply to children (group, location, owner, tags, map); otherwise every action that applies
 * to one of its kinds. Actions already on the rule are left out.
 */
export function pickableActions(catalog: RuleCatalog | null | undefined, kinds: AssetKind[], used: Iterable<string> = []): CatalogAction[] {
  const taken = new Set(used)
  const withChildren = kinds.includes('child')
  return (catalog?.actions ?? []).filter(
    (a) => !taken.has(a.key) && (withChildren ? a.asset_kinds.includes('child') : a.asset_kinds.some((k) => kinds.includes(k))),
  )
}

export const ENFORCEMENT_LABEL: Record<Enforcement, string> = { set_once: 'Set once', enforce: 'Enforce' }
export const ENFORCEMENT_HELP: Record<Enforcement, string> = {
  set_once: 'A value someone changed by hand after the rule set it is kept.',
  enforce: "The rule puts its value back when someone changes it by hand.",
}

/** "Runs on scans and new devices and servers · set once" (rule list line). */
export function ruleRunsText(rule: { triggers?: string[] | null; enforcement?: string | null; on_unmatch?: string | null }): string {
  const t = (rule.triggers?.length ? rule.triggers : ['discovery']).map((k) => TRIGGER_SHORT[k] ?? k)
  const enf = rule.enforcement === 'enforce' ? 'enforced' : 'set once'
  const unmatch = rule.on_unmatch === 'revert' ? ' · reverts when no longer matching' : ''
  return `runs on ${joinWords(t, 'and')} · ${enf}${unmatch}`
}

const SOURCE_WORD: Record<FieldSource, string> = { scan: 'scans', device: 'devices', server: 'servers', child: 'children' }

/**
 * Where a condition field has a value, as a short hint for the field picker: "" when it is known everywhere,
 * "scan only", "devices only", "devices and servers", "children only". A catalog without applies_to (phase 1 API)
 * means scans only. Controller-managed children count only for a rule that includes them (`kinds`): then a field
 * known everywhere but on children says "not children".
 */
export function fieldSourceHint(field: Pick<CatalogField, 'applies_to'> | undefined, kinds?: AssetKind[]): string {
  const withChildren = !!kinds?.includes('child')
  const universe: FieldSource[] = withChildren ? ['scan', 'device', 'server', 'child'] : ['scan', 'device', 'server']
  const all = field?.applies_to ?? ['scan']
  if (all.length === 0) return ''
  const inUniverse = all.filter((k) => universe.includes(k))
  const src = inUniverse.length ? inUniverse : all
  if (universe.every((k) => src.includes(k))) return ''
  if (withChildren && src.length === 3 && !src.includes('child')) return 'not children'
  if (src.length === 1) return src[0] === 'scan' ? 'scan only' : `${SOURCE_WORD[src[0]]} only`
  return joinWords(src.map((k) => SOURCE_WORD[k] ?? k), 'and')
}

// ── server inventory conditions (services, software, watched processes) ────

/** What an inventory field's names are, for the builder hint ("has" matches it exactly). */
const INVENTORY_NAME: Record<string, string> = {
  services: 'service name or display name',
  running_services: 'service name or display name',
  software: 'software name',
  software_publishers: 'publisher name',
  watched_processes: 'process name',
}

/** Example values per inventory field: [list ("has any of"), text ("has one containing")]. */
const INVENTORY_EXAMPLE: Record<string, [string, string]> = {
  services: ['MSSQLSERVER, W3SVC', 'SQL Server'],
  running_services: ['MSSQLSERVER, W3SVC', 'SQL Server'],
  software: ['Microsoft SQL Server 2019 (64-bit)', 'SQL Server'],
  software_publishers: ['Microsoft Corporation, Oracle Corporation', 'Oracle'],
  watched_processes: ['sqlservr.exe, nginx', 'java'],
}

export function isInventoryField(field: Pick<CatalogField, 'type'> | null | undefined): boolean {
  return field?.type === 'inventory'
}

/**
 * The hint under an inventory condition: names match ignoring case, "has" the exact name, "has one containing" part
 * of one; a server that reported nothing for the field fails every test but "is not reported".
 */
export function inventoryHint(field: Pick<CatalogField, 'key' | 'type'> | null | undefined, operator: string): string {
  if (!isInventoryField(field)) return ''
  const name = INVENTORY_NAME[field!.key] ?? 'name'
  const match =
    operator === 'has' || operator === 'has_none'
      ? `Matches the exact ${name}, ignoring case.`
      : operator === 'has_containing' || operator === 'has_none_containing'
        ? `Matches part of a ${name}, ignoring case.`
        : ''
  const missing =
    'Not reported: the server\'s agent sent nothing for it (servers without an agent report nothing, Linux servers no ' +
    'software list, older Linux agents no services) or has not refreshed it for 24 hours; every other test, “has none” included, is then false.'
  return match ? `${match} ${missing}` : missing
}

/** Placeholder for an inventory condition's value. */
export function inventoryPlaceholder(fieldKey: string, kind: OperatorValueKind | string): string {
  const ex = INVENTORY_EXAMPLE[fieldKey]
  if (!ex) return kind === 'list' ? 'Comma separated' : ''
  return kind === 'list' ? ex[0] : ex[1]
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
    if (spec.key === 'map' || spec.key === 'service_template') {
      // Placing an asset on a map is a map edit, whatever the asset; a check from a template is a service check edit.
      spec.permission.forEach((p) => needed.add(p))
      continue
    }
    const applies = spec.asset_kinds.filter((k) => kinds.has(k))
    if (applies.includes('device') || applies.includes('child')) spec.permission.filter((p) => p !== 'servers.manage').forEach((p) => needed.add(p))
    if (applies.includes('server')) needed.add('servers.manage')
  }
  return Array.from(needed).sort()
}

// ── phase 2: inventory runs ─────────────────────────────────────────────────

export const CHANGE_OUTCOME_LABEL: Record<ChangeOutcome, string> = {
  apply: 'Will change',
  revert: 'Revert',
  kept_manual_edit: 'Hand edit kept',
  not_reversible: 'Left alone',
}
export const CHANGE_OUTCOME_TONE: Record<ChangeOutcome, Tone> = {
  apply: 'success',
  revert: 'warning',
  kept_manual_edit: 'info',
  not_reversible: 'warning',
}
export const CHANGE_OUTCOME_ORDER: ChangeOutcome[] = ['apply', 'revert', 'kept_manual_edit', 'not_reversible']

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function kindNoun(kinds: AssetKind[] | undefined, n: number): string {
  const k = new Set<AssetKind>(kinds?.length ? kinds : ['device', 'server'])
  // Controller-managed children are devices too.
  const dev = k.has('device') || k.has('child')
  if (dev && !k.has('server')) return n === 1 ? 'device' : 'devices'
  if (k.has('server') && !dev) return n === 1 ? 'server' : 'servers'
  return n === 1 ? 'device or server' : 'devices and servers'
}

/** "42 devices and servers match; 17 values would change and 2 would revert on 9 assets; 3 hand edits kept" */
export function summarizeInventoryPreview(summary: Partial<InventorySummary>, kinds?: AssetKind[]): string {
  const matched = Number(summary.matched ?? 0)
  const changes = Number(summary.changes ?? 0)
  const reverts = Number(summary.reverts ?? 0)
  const assets = Number(summary.assets_changed ?? 0)
  const kept = Number(summary.kept_manual_edit ?? 0)
  const notRev = Number(summary.not_reversible ?? 0)
  const parts = [`${matched} ${kindNoun(kinds, matched)} ${matched === 1 ? 'matches' : 'match'}`]
  const on = `on ${plural(assets, 'asset')}`
  if (changes && reverts) parts.push(`${plural(changes, 'value')} would change and ${reverts} would revert ${on}`)
  else if (changes) parts.push(`${plural(changes, 'value')} would change ${on}`)
  else if (reverts) parts.push(`${plural(reverts, 'value')} would revert ${on}`)
  else parts.push('nothing would change')
  if (kept) parts.push(`${plural(kept, 'hand edit')} kept`)
  if (notRev) parts.push(`${notRev} left alone (not reversible)`)
  return parts.join('; ')
}

/** The apply dialog's sentence: "17 values will change and 2 will be reverted on 9 devices and servers." */
export function applyConfirmText(summary: Partial<InventorySummary>): string {
  const changes = Number(summary.changes ?? 0)
  const reverts = Number(summary.reverts ?? 0)
  const on = `on ${plural(Number(summary.assets_changed ?? 0), 'device or server', 'devices and servers')}`
  if (changes && reverts) return `${sentenceCase(plural(changes, 'value'))} will change and ${reverts} will be reverted ${on}.`
  if (reverts) return `${sentenceCase(plural(reverts, 'value'))} will be reverted ${on}.`
  return `${sentenceCase(plural(changes, 'value'))} will change ${on}.`
}

/** "Changed 17 values · reverted 2 · 3 hand edits kept · 1 skipped (changed since the preview)" */
export function summarizeApplyResult(r: Partial<InventoryApplyResult>): string {
  const parts = [`Changed ${plural(Number(r.applied ?? 0), 'value')}`]
  if (r.reverted) parts.push(`reverted ${r.reverted}`)
  if (r.kept_manual_edit) parts.push(`${plural(r.kept_manual_edit, 'hand edit')} kept`)
  if (r.not_reversible) parts.push(`${r.not_reversible} left alone (not reversible)`)
  if (r.skipped_changed) parts.push(`${r.skipped_changed} skipped (changed since the preview)`)
  if (r.not_ready) parts.push(`${r.not_ready} waiting for a sensor to come online`)
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
        : run.trigger === 'changed' ? 'Rules re-checked'
          : run.trigger === 'scheduled' ? 'Scheduled run'
            : run.trigger === 'undo' ? 'Undo'
            : run.discovery_run_id ? 'Re-evaluated' : 'Run on inventory'
  if (run.trigger === 'undo') return what
  const how = run.mode === 'apply' ? 'apply' : run.mode === 'simulate' ? 'simulate' : 'annotate only'
  return `${what} · ${how}`
}

/** Second line of the trigger column: why the run happened. */
export const TRIGGER_REASON: Record<string, string> = {
  discovery: 'Scan finished',
  created: 'Asset added',
  changed: 'Asset or rule changed',
  scheduled: 'Scheduled time (or Run now)',
  manual: 'Started by hand',
  undo: 'Undo of an earlier run',
}

export const RUN_STATUS_LABEL: Record<EngineRunStatus, string> = {
  running: 'Running',
  completed: 'Completed',
  partial: 'Partial',
  failed: 'Failed',
  undone: 'Undone',
  needs_approval: 'Waiting for approval',
  rejected: 'Rejected',
}

export function runStatusLabel(status: string): string {
  return (RUN_STATUS_LABEL as Record<string, string>)[status] ?? status.replace(/_/g, ' ')
}

export function runStatusTone(status: string): Tone {
  return status === 'completed' ? 'success'
    : status === 'partial' || status === 'needs_approval' ? 'warning'
      : status === 'failed' ? 'danger' : 'outline'
}

/** Values an automatic re-check holds for approval: changes plus reverts. */
export function pendingCount(run: { counts?: Record<string, any> | null }): number {
  const c = run.counts || {}
  return Number(c.changes ?? 0) + Number(c.reverts ?? 0)
}

/** Banner text over the runs that wait: "120 changes wait for approval" (two runs: "… in 2 runs"). */
export function waitingText(runs: { status: string; counts?: Record<string, any> | null }[]): string | null {
  const waiting = runs.filter((r) => r.status === 'needs_approval')
  if (!waiting.length) return null
  const n = waiting.reduce((sum, r) => sum + pendingCount(r), 0)
  const verb = n === 1 ? 'waits' : 'wait'
  return `${plural(n, 'change')} ${verb} for approval${waiting.length > 1 ? ` in ${waiting.length} runs` : ''}`
}

/** "Applied 118 values · reverted 2 · 3 skipped (changed since the plan)" or "Rejected". */
export function summarizeApprovalResult(r: Partial<ApprovalResult>): string {
  if (r.status === 'rejected') return 'Rejected: not proposed again until an asset or a rule changes'
  const parts = [`Applied ${plural(Number(r.applied ?? 0), 'value')}`]
  if (r.reverted) parts.push(`reverted ${r.reverted}`)
  if (r.kept_manual_edit) parts.push(`${plural(r.kept_manual_edit, 'hand edit')} kept`)
  if (r.not_reversible) parts.push(`${r.not_reversible} left alone (not reversible)`)
  if (r.skipped_changed) parts.push(`${r.skipped_changed} skipped (changed since the plan)`)
  if (r.not_ready) parts.push(`${r.not_ready} waiting for a sensor to come online`)
  return parts.join(' · ')
}

/** The file name an export downloads as: discovery-rules-2026-11-17.json (local date). */
export function exportFileName(date: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `discovery-rules-${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}.json`
}

/** A chosen file's text as an export document, or the reason it is not one (checked again by the API). */
export function parseRulesFile(text: string): { document: Record<string, any> } | { error: string } {
  let doc: any
  try {
    doc = JSON.parse(text)
  } catch {
    return { error: 'The file is not valid JSON.' }
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc) || doc.format !== 'zenplus-discovery-rules' || !Array.isArray(doc.rules)) {
    return { error: 'This is not a ZenPlus discovery rules export.' }
  }
  return { document: doc }
}

export function assetPath(kind: string | null | undefined, id: string | null | undefined): string | null {
  if (!id) return null
  // A controller-managed child has its own device page.
  if (kind === 'device' || kind === 'child') return `/devices/${id}`
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
 * Pre-selected assets from the run page's address: ?device=<id>, ?devices=<id,id>, ?server=<id>, ?servers=<id,id>,
 * ?child=<id>, ?children=<id,id> (controller-managed children). Anything that is not an id is dropped. `kinds`
 * follows what was pre-selected; nothing pre-selected means devices and servers.
 */
export function parseRunTargets(search: string | URLSearchParams): {
  device_ids: string[]
  server_ids: string[]
  child_ids: string[]
  kinds: AssetKind[]
} {
  const p = typeof search === 'string' ? new URLSearchParams(search) : search
  const device_ids = idList(p.get('device'), p.get('devices'))
  const server_ids = idList(p.get('server'), p.get('servers'))
  const child_ids = idList(p.get('child'), p.get('children'))
  const any = device_ids.length + server_ids.length + child_ids.length > 0
  const kinds: AssetKind[] = []
  if (device_ids.length || !any) kinds.push('device')
  if (server_ids.length || !any) kinds.push('server')
  if (child_ids.length) kinds.push('child')
  return { device_ids, server_ids, child_ids, kinds }
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
  child_ids?: string[]
  group_ids?: string[]
  tags?: string[]
  subnet?: string
  query?: string
}): InventoryTargets {
  const t: InventoryTargets = { kinds: f.kinds }
  if (f.device_ids?.length && f.kinds.includes('device')) t.device_ids = f.device_ids
  if (f.server_ids?.length && f.kinds.includes('server')) t.server_ids = f.server_ids
  if (f.child_ids?.length && f.kinds.includes('child')) t.child_ids = f.child_ids
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
  map: 'Network map',
  dependency_parent: 'Depends on (uplink parent)',
  interfaces: 'Interface monitoring',
  sensor: 'Monitor from sensor',
  agent_policy: 'Agent policy',
  service_template: 'Service check from template',
}

/** A setting value for people: group / template / credential names, On / Off, "not set". */
export function formatSettingValue(actionKey: string | null | undefined, value: any, lookups?: Lookups): string {
  if (value === null || value === undefined || value === '') return 'not set'
  if (actionKey === 'interfaces' && typeof value === 'boolean') return value ? 'monitored' : 'ignored'
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

/**
 * "Before → after" for a change or a history item (tags, maps and uplink parents show "+ added" / "− removed"; an
 * interface shows its name: "Interface gi1/0/2: monitored → ignored").
 */
export function changeText(actionKey: string | null | undefined, before: any, after: any, lookups?: Lookups, itemKey?: string | null): string {
  if (actionKey === 'interfaces') {
    return `${interfaceLabel(itemKey)}: ${formatSettingValue(actionKey, before, lookups)} → ${formatSettingValue(actionKey, after, lookups)}`
  }
  if (actionKey && SET_ACTION_KEYS.has(actionKey)) {
    if (before == null && after != null) return `+ ${formatSettingValue(actionKey, after, lookups)}`
    if (after == null && before != null) return `− ${formatSettingValue(actionKey, before, lookups)}`
  }
  return `${formatSettingValue(actionKey, before, lookups)} → ${formatSettingValue(actionKey, after, lookups)}`
}

/**
 * What a planned change does, for the preview and approval tables: "not set → Datacenter firewalls", "+ datacenter",
 * and for a revert (the asset no longer matches a rule set to revert) "Revert to Branch (now Datacenter)" or, for a
 * tag, map or uplink parent the rule added, "Remove datacenter".
 */
export function proposedText(
  c: Pick<InventoryChange, 'action_key' | 'item_key' | 'before' | 'after' | 'outcome'>,
  lookups?: Lookups,
): string {
  if (c.outcome !== 'revert') return changeText(c.action_key, c.before, c.after, lookups, c.item_key)
  if (c.action_key === 'interfaces') {
    return `${interfaceLabel(c.item_key)}: revert to ${formatSettingValue(c.action_key, c.after, lookups)} (now ${formatSettingValue(c.action_key, c.before, lookups)})`
  }
  if (SET_ACTION_KEYS.has(c.action_key) && c.after == null) {
    return `Remove ${formatSettingValue(c.action_key, c.before ?? c.item_key, lookups)}`
  }
  const now = formatSettingValue(c.action_key, c.before, lookups)
  return `Revert to ${formatSettingValue(c.action_key, c.after, lookups)} (now ${now})`
}

/** Where a setting's value opens: a network map, the uplink parent device. */
export function settingValueLink(actionKey: string | null | undefined, value: unknown): string | null {
  const id = typeof value === 'string' ? value : value == null ? '' : String(value)
  if (!id) return null
  if (actionKey === 'map') return `/maps/manual?map=${encodeURIComponent(id)}`
  if (actionKey === 'dependency_parent') return `/devices/${encodeURIComponent(id)}`
  return null
}

/** Ids of uplink parents named in these actions or changes, so the page can load their device names. */
export function dependencyIds(items: { key?: string; action_key?: string | null; value?: any; before?: any; after?: any; item_key?: string | null }[]): string[] {
  const out = new Set<string>()
  for (const it of items) {
    if ((it.key ?? it.action_key) !== 'dependency_parent') continue
    for (const v of [it.value, it.before, it.after, it.item_key]) {
      if (typeof v === 'string' && v) out.add(v.toLowerCase())
    }
  }
  return Array.from(out).sort()
}

/** "Datacenter's Device group is overridden by Fortinet firewalls on 12 assets" */
export function overriddenText(
  o: { rule_name: string | null; winner_rule_name: string | null; action_key: string; assets: number },
  catalog: RuleCatalog | null | undefined,
): string {
  return `${o.rule_name ?? 'A rule'}: ${settingLabel(o.action_key, catalog).toLowerCase()} is overridden by ${o.winner_rule_name ?? 'an earlier rule'} on ${plural(o.assets, 'asset')}`
}

// ── phase 4: interfaces, sensors, schedules, unmatched assets ───────────────

export const MAX_INTERFACE_PATTERNS = 10
export const MAX_INTERFACE_PATTERN = 200

export const INTERFACE_FIELD_ORDER: InterfacePattern['field'][] = ['if_name', 'if_alias', 'if_descr', 'if_type', 'speed']
/** Short field names for one-line summaries. */
export const INTERFACE_FIELD_WORD: Record<InterfacePattern['field'], string> = {
  if_name: 'name',
  if_alias: 'alias',
  if_descr: 'ifDescr',
  if_type: 'type',
  speed: 'speed',
}
/** Labels when the catalog does not give them (an older API). */
export const INTERFACE_FIELD_LABEL: Record<InterfacePattern['field'], string> = {
  if_name: 'Name (ifName)',
  if_alias: 'Alias / description set on the port',
  if_descr: 'ifDescr',
  if_type: 'IF-MIB type numbers (6, 24, 131, ...)',
  speed: 'Speed in Mbit/s (1000, 100-1000, 10000-, -100)',
}
export const INTERFACE_PLACEHOLDER: Record<InterfacePattern['field'], string> = {
  if_name: '^(gi|te)\\d',
  if_alias: 'uplink|wan',
  if_descr: '^(Loopback|Null|Vlan)',
  if_type: '6, 117',
  speed: '1000-',
}
export const INTERFACE_SET_LABEL: Record<InterfacePattern['set'], string> = { monitor: 'Monitor', ignore: 'Ignore' }

export function defaultInterfacePattern(): InterfacePattern {
  return { set: 'monitor', field: 'if_name', pattern: '' }
}

/**
 * A speed pattern in Mbit/s as the API reads it: "1000" (exactly), "100-1000", "10000-" (at least), "-100" (at most).
 * null when it is not one of those.
 */
export function parseSpeedRange(pattern: string): { min: number | null; max: number | null } | null {
  const p = String(pattern ?? '').replace(/\s+/g, '')
  const at = p.indexOf('-')
  const lo = at < 0 ? p : p.slice(0, at)
  const hi = at < 0 ? p : p.slice(at + 1)
  const num = (t: string) => (t === '' ? null : /^\d+(\.\d+)?$/.test(t) ? Number(t) : NaN)
  const min = num(lo)
  const max = num(hi)
  if (Number.isNaN(min) || Number.isNaN(max)) return null
  if (min === null && max === null) return null
  if (min !== null && max !== null && min > max) return null
  return { min, max }
}

/** Client-side checks of an interfaces action's patterns (the server checks again, regexes included). */
export function interfacePatternProblems(patterns: InterfacePattern[], max = MAX_INTERFACE_PATTERNS): string[] {
  const out: string[] = []
  if (!patterns.length) out.push('add at least one pattern')
  if (patterns.length > max) out.push(`at most ${max} patterns`)
  patterns.forEach((p, i) => {
    const n = `pattern ${i + 1}`
    const text = String(p?.pattern ?? '').trim()
    if (p?.set !== 'monitor' && p?.set !== 'ignore') out.push(`${n}: monitor or ignore`)
    if (!INTERFACE_FIELD_ORDER.includes(p?.field)) out.push(`${n}: choose what it tests`)
    if (!text) {
      out.push(`${n}: a pattern is required`)
      return
    }
    if (text.length > MAX_INTERFACE_PATTERN) out.push(`${n}: at most ${MAX_INTERFACE_PATTERN} characters`)
    if (p.field === 'if_type') {
      const parts = text.split(',').map((x) => x.trim()).filter(Boolean)
      if (!parts.length || !parts.every((x) => /^\d+$/.test(x))) out.push(`${n}: IF-MIB type numbers separated by commas (6, 24, 131)`)
    } else if (p.field === 'speed' && !parseSpeedRange(text)) {
      out.push(`${n}: Mbit/s as 1000, 100-1000, 10000- or -100`)
    }
  })
  return out
}

/** "monitor where name matches ^gi", "ignore where type is 24 or 131", "ignore where speed is at most 100 Mbit/s" */
export function interfacePatternText(p: InterfacePattern): string {
  const set = p.set === 'ignore' ? 'ignore' : 'monitor'
  const word = INTERFACE_FIELD_WORD[p.field] ?? p.field
  const text = String(p.pattern ?? '').trim()
  if (p.field === 'if_type') {
    return `${set} where type is ${joinWords(text.split(',').map((x) => x.trim()).filter(Boolean), 'or')}`
  }
  if (p.field === 'speed') {
    const r = parseSpeedRange(text)
    if (!r) return `${set} where speed is ${text}`
    if (r.min !== null && r.max !== null) {
      return r.min === r.max ? `${set} where speed is ${r.min} Mbit/s` : `${set} where speed is ${r.min}–${r.max} Mbit/s`
    }
    return r.min !== null ? `${set} where speed is at least ${r.min} Mbit/s` : `${set} where speed is at most ${r.max} Mbit/s`
  }
  return `${set} where ${word} matches ${text}`
}

/** One line for a rule's interface patterns, in test order. */
export function summarizeInterfacePatterns(patterns: InterfacePattern[]): string {
  if (!patterns.length) return 'no patterns'
  return patterns.map(interfacePatternText).join('; ')
}

/** An interface's item key as people read it: "Interface gi1/0/2", "Interface ifIndex 12" for "#12". */
export function interfaceLabel(itemKey: string | null | undefined): string {
  const k = String(itemKey ?? '')
  if (!k) return 'Interface'
  return k.startsWith('#') ? `Interface ifIndex ${k.slice(1)}` : `Interface ${k}`
}

/** A run item's outcome label; a sensor that is not online yet waits instead of "Not ready". */
export function itemOutcomeLabel(outcome: string, actionKey?: string | null): string {
  if (outcome === 'not_ready' && actionKey === 'sensor') return 'Waiting: sensor not online'
  return (OUTCOME_LABEL as Record<string, string>)[outcome] ?? outcome.replace(/_/g, ' ')
}

// Schedules

export const SCHEDULE_HOURS = [1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168]

/** "Every hour", "Every 6 hours", "Daily", "Every 2 days", "Weekly (Mondays)". */
export function scheduleEveryText(hours: number): string {
  if (hours === 1) return 'Every hour'
  if (hours < 24) return `Every ${hours} hours`
  if (hours === 24) return 'Daily'
  if (hours === 168) return 'Weekly (Mondays)'
  if (hours % 24 === 0) return `Every ${hours / 24} days`
  return `Every ${hours} hours`
}

/**
 * The schedule as a sentence: "Every 6 hours from 02:00 (Asia/Riyadh)", "Daily at 02:00 (UTC)", "Weekly on Mondays at
 * 02:00 (UTC)". Intervals under a day repeat from the time each day; longer ones every N days at the time.
 */
export function scheduleText(s: Pick<RuleEngineSettings, 'schedule_every_hours' | 'schedule_time' | 'schedule_timezone'>): string {
  const every = Number(s.schedule_every_hours ?? 24)
  const at = s.schedule_time || '02:00'
  const tz = s.schedule_timezone || 'UTC'
  if (every < 24) return `${scheduleEveryText(every)} from ${at} (${tz})`
  if (every === 24) return `Daily at ${at} (${tz})`
  if (every === 168) return `Weekly on Mondays at ${at} (${tz})`
  return `${scheduleEveryText(every)} at ${at} (${tz})`
}

export function isScheduleTime(text: string | null | undefined): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(String(text ?? ''))
}

/** A time zone name the browser knows (the server checks it against its own zone database). */
export function isTimeZone(tz: string | null | undefined): boolean {
  const z = String(tz ?? '').trim()
  if (!z || z.length > 64) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: z })
    return true
  } catch {
    return false
  }
}

/** The browser's own IANA time zone, for a schedule being turned on; UTC when it cannot tell. */
export function browserTimeZone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    return tz && isTimeZone(tz) ? tz : 'UTC'
  } catch {
    return 'UTC'
  }
}

/** Problems with the phase 4 settings (growth stop, schedule) before they are saved. */
export function engineSettingsProblems(s: Partial<RuleEngineSettings>, hours: number[] = SCHEDULE_HOURS): string[] {
  const out: string[] = []
  const int = (v: unknown, lo: number, hi: number) => Number.isInteger(v) && (v as number) >= lo && (v as number) <= hi
  if (s.growth_breaker_pct != null && !int(s.growth_breaker_pct, 0, 10000)) out.push('Growth stop: a percentage from 0 (off) to 10000')
  if (s.growth_breaker_min != null && !int(s.growth_breaker_min, 0, 100000)) out.push('Growth stop: a minimum from 0 to 100000 assets')
  if (s.schedule_enabled) {
    if (!hours.includes(Number(s.schedule_every_hours))) out.push('Schedule: choose how often')
    if (!isScheduleTime(s.schedule_time)) out.push('Schedule: a time as HH:MM')
    if (!isTimeZone(s.schedule_timezone)) out.push('Schedule: a known time zone, such as Asia/Riyadh or UTC')
  }
  return out
}

/** "in 3 h 20 min", "in 2 days", "now" — how long until an ISO time. */
export function untilText(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const min = Math.round((t - now) / 60000)
  if (min <= 0) return 'now'
  if (min < 60) return `in ${min} min`
  const h = Math.floor(min / 60)
  if (h < 48) return min % 60 ? `in ${h} h ${min % 60} min` : `in ${h} h`
  return `in ${Math.round(h / 24)} days`
}

export const SCHEDULE_SKIP_TEXT: Record<string, string> = {
  engine_off: 'Discovery rules are off',
  schedule_off: 'The schedule is off',
  schedule_started: 'The schedule starts at the next scheduled time',
  not_due: 'Not due yet',
  no_rules: 'No active or simulating rule has the scheduled trigger',
  waiting_for_approval: 'An earlier scheduled run still waits for approval',
}

/** A "Run scheduled rules now" answer for a toast: applied, held for approval, or skipped. */
export function scheduleRunText(r: ScheduleRunResult): string {
  if (r.skipped) return SCHEDULE_SKIP_TEXT[r.skipped] ?? sentenceCase(r.skipped.replace(/_/g, ' '))
  if (r.needs_approval) {
    const n = Number(r.changes ?? 0)
    return `${sentenceCase(plural(n, 'change'))} ${n === 1 ? 'waits' : 'wait'} for approval${r.reasons?.length ? `: ${r.reasons.join('; ')}` : ''}`
  }
  const parts = [`Checked ${plural(Number(r.assets ?? 0), 'asset')}`, `matched ${Number(r.matched ?? 0)}`, `applied ${plural(Number(r.applied ?? 0), 'value')}`]
  if (r.reverted) parts.push(`reverted ${r.reverted}`)
  if (r.kept_manual_edit) parts.push(`${plural(r.kept_manual_edit, 'hand edit')} kept`)
  if (r.skipped_changed) parts.push(`${r.skipped_changed} skipped (changed since)`)
  if (r.not_ready) parts.push(`${r.not_ready} waiting for a sensor to come online`)
  return parts.join(' · ')
}

/** "12 of 300 network devices · 3 of 40 servers" (kinds in the usual order). */
export function unmatchedKindsText(report: Pick<UnmatchedReport, 'kinds'>): string {
  return ASSET_KIND_ORDER.filter((k) => report.kinds?.[k])
    .map((k) => {
      const c = report.kinds[k]!
      return `${c.unmatched} of ${c.assets} ${KIND_PLURAL[k]}`
    })
    .join(' · ')
}
