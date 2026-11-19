// Parent/child dependencies page model (docs/dependencies.md). No imports, so
// scripts/test-dependencies-ui.mjs runs it without the app.

export type DepSource = 'manual' | 'topology' | 'discovery_rule' | 'controller'

export interface DepEnd {
  kind: 'device' | 'server'
  id: string
  name: string | null
  ip: string | null
  status: string | null
  type: string | null
}

export interface Dependency {
  id: string
  dependency_type: string
  suppress_alerts: boolean
  enabled: boolean
  notes: string | null
  source: DepSource
  source_label: string
  removable: boolean
  created_at: string | null
  created_by: string | null
  parent: DepEnd
  child: DepEnd
}

export interface Suggestion {
  parent: DepEnd
  child: DepEnd
  hops: number
  parent_interface: string | null
  child_interface: string | null
  protocol: string | null
  last_seen_at: string | null
}

export interface SuggestionResult {
  roots: (DepEnd & { reason: 'gateway' | 'lowest_rtt' | 'chosen' })[]
  suggestions: Suggestion[]
  redundant: { child: DepEnd; parents: DepEnd[] }[]
  unrooted_components: number
  gateways: string[]
}

export const SOURCE_BADGE: Record<DepSource, { label: string; variant: 'outline' | 'info' | 'default' }> = {
  manual: { label: 'Added by hand', variant: 'outline' },
  topology: { label: 'From topology', variant: 'info' },
  discovery_rule: { label: 'Discovery rule', variant: 'default' },
  controller: { label: 'Controller', variant: 'default' },
}

export const DEPENDENCY_TYPES = ['uplink', 'wan', 'power', 'site', 'service', 'manual'] as const

/** "core-1 (10.0.0.1)": the name and the address, each once. */
export function endLabel(e: Pick<DepEnd, 'name' | 'ip'>): string {
  const name = (e.name || '').trim()
  const ip = (e.ip || '').trim()
  if (!ip || ip === name) return name || ip || 'unknown'
  return name ? `${name} (${ip})` : ip
}

/** Why a row cannot be removed here, or null when it can. */
export function ownerNote(d: Pick<Dependency, 'source' | 'removable'>): string | null {
  if (d.removable) return null
  if (d.source === 'discovery_rule') return 'Written by a discovery rule: change or undo the rule to remove it.'
  if (d.source === 'controller') return 'Kept by the controller sync for a managed child: it follows the controller.'
  return 'Owned by another part of ZenPlus.'
}

/** Whether the row holds the child's alerts while the parent is down. */
export function holdsAlerts(d: Pick<Dependency, 'enabled' | 'suppress_alerts'>): boolean {
  return d.enabled && d.suppress_alerts
}

export function rootReasonText(reason: string): string {
  if (reason === 'gateway') return "this appliance's default gateway"
  if (reason === 'lowest_rtt') return 'the lowest ping round-trip time from this appliance'
  if (reason === 'chosen') return 'chosen by you'
  return reason
}

/** "via Gi1/0/48 to Gi0/1 (LLDP)". */
export function viaText(s: Pick<Suggestion, 'parent_interface' | 'child_interface' | 'protocol'>): string {
  const ports = [s.parent_interface, s.child_interface].filter(Boolean) as string[]
  const how = s.protocol ? s.protocol.toUpperCase() : ''
  if (!ports.length) return how ? `seen by ${how}` : ''
  return `via ${ports.join(' to ')}${how ? ` (${how})` : ''}`
}

/** Case-insensitive match on either end's name or address. */
export function filterDependencies(rows: Dependency[], query: string): Dependency[] {
  const q = query.trim().toLowerCase()
  if (!q) return rows
  return rows.filter((d) => [d.parent.name, d.parent.ip, d.child.name, d.child.ip]
    .some((v) => (v || '').toLowerCase().includes(q)))
}

/** Rows grouped by parent, parents in name order, for the list. */
export function byParent(rows: Dependency[]): { parent: DepEnd; children: Dependency[] }[] {
  const groups = new Map<string, { parent: DepEnd; children: Dependency[] }>()
  for (const d of rows) {
    const g = groups.get(d.parent.id) || { parent: d.parent, children: [] }
    g.children.push(d)
    groups.set(d.parent.id, g)
  }
  const byName = (a: DepEnd, b: DepEnd) => endLabel(a).localeCompare(endLabel(b))
  return [...groups.values()]
    .map((g) => ({ ...g, children: [...g.children].sort((a, b) => byName(a.child, b.child)) }))
    .sort((a, b) => byName(a.parent, b.parent))
}

/** The POST body that accepts a suggestion. */
export function acceptBody(s: Pick<Suggestion, 'parent' | 'child'>) {
  return { parent_device_id: s.parent.id, child_device_id: s.child.id, dependency_type: 'uplink', source: 'topology' }
}
