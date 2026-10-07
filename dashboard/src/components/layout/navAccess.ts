// Which navigation entries a role may see, and whether it may open a page.
//
// Kept free of React and icon imports so the same rules can be exercised by
// scripts/test-nav-access.mjs. The server remains the authorization boundary
// (route policy + per-endpoint guards); these rules keep the sidebar, the
// breadcrumbs and direct URLs in step with what the server will allow, so a
// role never sees a menu it cannot open.

export type Can = (permission: string) => boolean

export type NavGate = {
  /** Visible when the role grants ANY of these. */
  permission?: string | readonly string[]
  /** Visible only when the role grants ALL of these (checked before `permission`). */
  requireAll?: readonly string[]
}

type GatedNode = NavGate & { to: string; children?: GatedNode[] }
type GatedGroup<N extends GatedNode> = { items: N[] }

function anyOf(permission: NavGate['permission']): readonly string[] {
  if (!permission) return []
  return typeof permission === 'string' ? [permission] : permission
}

/** The node's own gate (its parent's gate is checked separately). */
export function gateAllows(gate: NavGate, can: Can): boolean {
  if (gate.requireAll && !gate.requireAll.every((p) => can(p))) return false
  const any = anyOf(gate.permission)
  return any.length === 0 || any.some((p) => can(p))
}

/** Every permission a gate mentions (for coverage checks and previews). */
export function gatePermissions(gate: NavGate): string[] {
  return [...(gate.requireAll || []), ...anyOf(gate.permission)]
}

/**
 * Removes nodes the role may not see. A child is only reachable through its
 * parent, so it needs the parent's gate as well as its own; a parent whose
 * children are all hidden is hidden too, and so is an empty group.
 */
export function filterNavGroups<N extends GatedNode, G extends GatedGroup<N>>(groups: readonly G[], can: Can): G[] {
  const keep = (node: N): N | null => {
    if (!gateAllows(node, can)) return null
    if (!node.children) return node
    const children = (node.children as N[]).map(keep).filter(Boolean) as N[]
    if (children.length === 0) return null
    return { ...node, children }
  }
  return groups
    .map((g) => ({ ...g, items: g.items.map(keep).filter(Boolean) as N[] }))
    .filter((g) => g.items.length > 0)
}

/** First page the role may open, in sidebar order (its landing page). */
export function firstAllowedPath<N extends GatedNode, G extends GatedGroup<N>>(groups: readonly G[], can: Can): string | null {
  for (const group of filterNavGroups(groups, can)) {
    for (const item of group.items) {
      const leaf = item.children?.[0] ?? item
      return leaf.to
    }
  }
  return null
}

/**
 * Whether the role may open the location a breadcrumb trail resolved to.
 * `selfGated` items (Settings) hide their own tabs and fall back to one the
 * role holds, so only the item itself is checked for them. Locations no
 * navigation entry owns are left to the page and the server.
 */
export function trailAllows(
  trail: { item: NavGate & { selfGated?: boolean }; child?: NavGate } | null,
  can: Can,
): boolean {
  if (!trail) return true
  if (!gateAllows(trail.item, can)) return false
  if (trail.child && !trail.item.selfGated && !gateAllows(trail.child, can)) return false
  return true
}

/** `can` for an arbitrary permission set — used to preview what a role being
 *  edited will see. `system.admin` implies everything, as on the server. */
export function canFromPermissions(permissions: Iterable<string>): Can {
  const set = new Set(permissions)
  return (p) => set.has('system.admin') || set.has(p)
}
