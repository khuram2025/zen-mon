// Pure helpers behind the role editor's per-module access levels.
// No React or API imports: scripts/test-role-model.mjs exercises them directly.

export type Risk = 'standard' | 'sensitive' | 'privileged' | 'critical'
export type ModulePermission = { id: string; label: string; description: string; risk?: Risk; implies?: string[] }
export type ModuleEntry = { module: string; label: string; area?: string; permissions: ModulePermission[] }

/** None / View / Full are presets over a module's standard-risk permissions;
 *  anything else is Custom. Sensitive, privileged and critical permissions
 *  never come with a preset — they are ticked one by one. */
export type AccessLevel = 'none' | 'view' | 'full' | 'custom'

/** Order of the product areas in the editor (the sidebar's order). */
export const AREA_ORDER = ['General', 'Network', 'Systems', 'Applications', 'Operations', 'Administration', 'Other']

export const AREA_HINTS: Record<string, string> = {
  General: 'The overview dashboards everyone starts from.',
  Network: 'Network devices, traffic, paths, endpoints and configuration.',
  Systems: 'Servers, their agents, policies and baselines.',
  Applications: 'Application performance monitoring: services, traces, errors, SLOs and RUM.',
  Operations: 'Shared by every team: service checks, alerts and reports.',
  Administration: 'Who can do what, the audit trail and appliance settings.',
  Other: '',
}

export const isElevatedRisk = (risk?: Risk) => risk === 'privileged' || risk === 'critical'

/** Permissions a preset may include: standard risk only. */
export const isPresetRisk = (risk?: Risk) => !risk || risk === 'standard'

function standardIds(m: ModuleEntry): string[] {
  return m.permissions.filter((p) => isPresetRisk(p.risk)).map((p) => p.id)
}

function viewIds(m: ModuleEntry): string[] {
  return standardIds(m).filter((id) => id.endsWith('.view'))
}

function sameSet(a: string[], b: string[]) {
  return a.length === b.length && a.every((x) => b.includes(x))
}

/** Which presets make sense for a module (View and Full collapse when equal).
 *  Empty: the module only has individually ticked permissions (Audit, Full
 *  administration). */
export function levelsFor(m: ModuleEntry): AccessLevel[] {
  const std = standardIds(m)
  if (std.length === 0) return []
  const view = viewIds(m)
  if (view.length === 0 || sameSet(view, std)) return ['none', 'full']
  return ['none', 'view', 'full']
}

/** Label of the 'full' preset: "View" when the module's presets are all read-only. */
export function fullLabel(m: ModuleEntry): string {
  const std = standardIds(m)
  return std.length > 0 && std.every((id) => id.endsWith('.view')) ? 'View' : 'Full'
}

/** Permissions shown as individual extras next to the presets. */
export function extraPermissions(m: ModuleEntry): ModulePermission[] {
  return m.permissions.filter((p) => !isPresetRisk(p.risk))
}

/** The preset the module's current selection matches (standard permissions only). */
export function moduleLevel(m: ModuleEntry, granted: ReadonlySet<string>): AccessLevel {
  const std = standardIds(m)
  const on = std.filter((id) => granted.has(id))
  const extraOn = m.permissions.some((p) => !isPresetRisk(p.risk) && granted.has(p.id))
  if (on.length === 0) return extraOn ? 'custom' : 'none'
  if (sameSet(on, std)) return 'full'
  const view = viewIds(m)
  if (view.length > 0 && sameSet(on, view)) return 'view'
  return 'custom'
}

/** Drops every granted permission whose prerequisites are no longer granted. */
export function pruneDangling(modules: readonly ModuleEntry[], granted: ReadonlySet<string>): Set<string> {
  const implies = new Map<string, string[]>()
  for (const m of modules) for (const p of m.permissions) implies.set(p.id, p.implies || [])
  const next = new Set(granted)
  let changed = true
  while (changed) {
    changed = false
    for (const id of [...next]) {
      if ((implies.get(id) || []).some((dep) => !next.has(dep))) { next.delete(id); changed = true }
    }
  }
  return next
}

/**
 * Apply a preset to one module. Only permissions the editor may grant are
 * added; individually ticked (sensitive / privileged) permissions already
 * granted are kept when the preset still satisfies what they depend on.
 */
export function applyLevel(
  modules: readonly ModuleEntry[],
  m: ModuleEntry,
  level: Exclude<AccessLevel, 'custom'>,
  granted: ReadonlySet<string>,
  canGrant: (id: string) => boolean = () => true,
): Set<string> {
  const next = new Set(granted)
  const std = standardIds(m)
  for (const id of std) next.delete(id)
  if (level === 'none') {
    for (const p of m.permissions) next.delete(p.id)
    return pruneDangling(modules, next)
  }
  const target = level === 'view' ? viewIds(m) : std
  for (const id of target) if (canGrant(id) || granted.has(id)) next.add(id)
  return pruneDangling(modules, next)
}

/** Modules grouped by area, in sidebar order. */
export function groupByArea<M extends ModuleEntry>(modules: readonly M[]): { area: string; modules: M[] }[] {
  const by = new Map<string, M[]>()
  for (const m of modules) {
    const area = m.area || 'Other'
    by.set(area, [...(by.get(area) || []), m])
  }
  return [...by.entries()]
    .sort(([a], [b]) => (AREA_ORDER.indexOf(a) + 1 || 99) - (AREA_ORDER.indexOf(b) + 1 || 99))
    .map(([area, ms]) => ({ area, modules: ms }))
}

/** Lowercased, trimmed, de-duplicated tag list, first spelling kept. */
export function cleanTags(tags: readonly string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of tags) {
    const t = raw.trim()
    if (!t || seen.has(t.toLowerCase())) continue
    seen.add(t.toLowerCase())
    out.push(t)
  }
  return out
}

export function sameTags(a: readonly string[], b: readonly string[]): boolean {
  const x = cleanTags(a).map((t) => t.toLowerCase()).sort()
  const y = cleanTags(b).map((t) => t.toLowerCase()).sort()
  return x.length === y.length && x.every((t, i) => t === y[i])
}

/** Grant `id` together with the permissions it depends on (view for manage, …). */
export function withDependencies(modules: readonly ModuleEntry[], granted: ReadonlySet<string>, id: string): Set<string> {
  const implies = new Map<string, string[]>()
  for (const m of modules) for (const p of m.permissions) implies.set(p.id, p.implies || [])
  const next = new Set(granted)
  const stack = [id]
  while (stack.length) {
    const cur = stack.pop()!
    if (next.has(cur)) continue
    next.add(cur)
    for (const dep of implies.get(cur) || []) if (!next.has(dep)) stack.push(dep)
  }
  return next
}
