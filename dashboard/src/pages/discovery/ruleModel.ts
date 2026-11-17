// Discovery rules: the pure model behind the rule builder, the rule list and the scan-result columns.
// No React and no API calls here, so scripts/test-discovery-rules.mjs can load it in node.
//
// A condition is a tree of groups ({op, conditions}) and leaves ({field, operator, value}); a path is the list of
// child indexes from the root group ([] is the root, [1, 0] the first child of the root's second child).

import type {
  CatalogField,
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
  return [...out, ...conditionProblems(draft.condition, catalog, catalog?.limits ?? DEFAULT_LIMITS), ...actionProblems(draft, catalog)]
}

// ── modes, dispositions, outcomes ───────────────────────────────────────────

export const MODE_LABEL: Record<RuleMode, string> = { draft: 'Draft', active: 'Active', disabled: 'Disabled' }
export const MODE_TONE: Record<RuleMode, Tone> = { draft: 'warning', active: 'success', disabled: 'outline' }

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
  simulated: 'Simulated',
  shadowed: 'Shadowed',
  blocked_license: 'Blocked: license',
  blocked_cap: 'Blocked: per-scan limit',
  not_ready: 'Not ready',
  error: 'Error',
}

export const OUTCOME_TONE: Record<EngineRunOutcome, Tone> = {
  applied: 'success',
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
  trigger: 'not run on discovery',
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
