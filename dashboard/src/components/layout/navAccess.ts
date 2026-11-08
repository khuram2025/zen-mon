// Which navigation entries a role may see, and whether it may open a page.
//
// Kept free of React and icon imports so the same rules can be exercised by
// scripts/test-nav-access.mjs. The server remains the authorization boundary
// (route policy + per-endpoint guards); these rules keep the sidebar, the
// breadcrumbs and direct URLs in step with what the server will allow, so a
// role never sees a menu it cannot open.
//
// A role may also be limited to selected pages (`roles.page_access.pages`, a
// list of the `to` of the leaf entries). The server applies the same limit to
// the role's permissions (app.core.page_access, fed by
// server/app/core/page_catalog.json, which scripts/test-page-access.mjs keeps
// in step with navigation.ts), so a hidden page's API is refused too.

export type Can = (permission: string) => boolean

export type NavGate = {
  /** Visible when the role grants ANY of these. */
  permission?: string | readonly string[]
  /** Visible only when the role grants ALL of these (checked before `permission`). */
  requireAll?: readonly string[]
  /** Read-only data the page also shows when the role holds it (dashboard widgets). A role limited to selected
   *  pages keeps these for the page, and nothing more of them. */
  uses?: readonly string[]
  /** The page only displays data (dashboards): a role limited to it keeps the view permissions it needs, never the
   *  manage or acknowledge permissions that extend them. */
  viewOnly?: boolean
  /** Personal page (appearance, support, own profile): open to every account and never limited by a page list. */
  personal?: boolean
}

type GatedNode = NavGate & { to: string; label?: string; children?: GatedNode[] }
type GatedGroup<N extends GatedNode> = { items: N[] }

/** Pages a role is limited to, by leaf `to`; null = every page its permissions allow. */
export type PageSet = ReadonlySet<string> | null

/** A leaf entry is on the role's page list (personal pages always are). */
export function pageListed(node: { to: string; personal?: boolean }, pages: PageSet): boolean {
  return !pages || !!node.personal || pages.has(node.to)
}

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
export function filterNavGroups<N extends GatedNode, G extends GatedGroup<N>>(groups: readonly G[], can: Can, pages: PageSet = null): G[] {
  const keep = (node: N): N | null => {
    if (!gateAllows(node, can)) return null
    if (!node.children) return pageListed(node, pages) ? node : null
    const children = (node.children as N[]).map(keep).filter(Boolean) as N[]
    if (children.length === 0) return null
    return { ...node, children }
  }
  return groups
    .map((g) => ({ ...g, items: g.items.map(keep).filter(Boolean) as N[] }))
    .filter((g) => g.items.length > 0)
}

/** First page the role may open, in sidebar order (its landing page). */
export function firstAllowedPath<N extends GatedNode, G extends GatedGroup<N>>(groups: readonly G[], can: Can, pages: PageSet = null): string | null {
  for (const group of filterNavGroups(groups, can, pages)) {
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
  trail: { item: NavGate & { to: string; selfGated?: boolean; children?: GatedNode[] }; child?: NavGate & { to: string } } | null,
  can: Can,
  pages: PageSet = null,
): boolean {
  if (!trail) return true
  if (!gateAllows(trail.item, can)) return false
  if (trail.child && !trail.item.selfGated && !gateAllows(trail.child, can)) return false
  if (!pages) return true
  // Settings falls back to a tab on the list itself (personal tabs always are).
  if (trail.item.selfGated) return (trail.item.children || []).some((c) => pageListed(c, pages))
  if (trail.child) return pageListed(trail.child, pages)
  // A section's own address (or a detail page it owns) opens when any of its pages is listed.
  if (trail.item.children?.length) return trail.item.children.some((c) => pageListed(c, pages))
  return pageListed(trail.item, pages)
}

/** `can` for an arbitrary permission set — used to preview what a role being
 *  edited will see. `system.admin` implies everything, as on the server. */
export function canFromPermissions(permissions: Iterable<string>): Can {
  const set = new Set(permissions)
  return (p) => set.has('system.admin') || set.has(p)
}

/* ------------------------------------------------------------------ */
/*  Page catalog and page-limited permissions                          */
/* ------------------------------------------------------------------ */

/** One page a role can be limited to: a leaf of the navigation tree. `gates` holds the parent's gate and the
 *  page's own (both must pass). Serialised to server/app/core/page_catalog.json. */
export type PageEntry = {
  key: string
  label: string
  group: string
  group_label: string
  parent?: string
  gates: { any?: string[]; all?: string[] }[]
  uses?: string[]
  view_only?: boolean
  personal?: boolean
}

function gateEntry(gate: NavGate): { any?: string[]; all?: string[] } | null {
  const any = [...anyOf(gate.permission)]
  const all = [...(gate.requireAll || [])]
  if (any.length === 0 && all.length === 0) return null
  return { ...(any.length ? { any } : {}), ...(all.length ? { all } : {}) }
}

/** Every page of the navigation tree, in sidebar order. */
export function pageCatalog<N extends GatedNode>(groups: readonly { id: string; label: string; items: N[] }[]): PageEntry[] {
  const out: PageEntry[] = []
  for (const g of groups) {
    for (const item of g.items) {
      const leaves = item.children?.length ? item.children : [item]
      for (const leaf of leaves) {
        const gates = [leaf === item ? null : gateEntry(item), gateEntry(leaf)].filter(Boolean) as PageEntry['gates']
        const uses = [...(leaf.uses || [])]
        out.push({
          key: leaf.to,
          label: leaf.label || leaf.to,
          group: g.id,
          group_label: g.label,
          ...(leaf === item ? {} : { parent: item.label || item.to }),
          gates,
          ...(uses.length ? { uses } : {}),
          ...(leaf.viewOnly ? { view_only: true } : {}),
          ...(leaf.personal ? { personal: true } : {}),
        })
      }
    }
  }
  return out
}

/** The role's permissions open this page (every gate passes). */
export function pageReachable(entry: PageEntry, can: Can): boolean {
  return entry.gates.every((g) => (!g.all || g.all.every((p) => can(p))) && (!g.any || g.any.length === 0 || g.any.some((p) => can(p))))
}

/**
 * Permissions a role keeps when it is limited to `pages`. Mirrors `app.core.page_access.restrict_permissions` on
 * the server, which is what the API enforces. A permission stays when
 *   - a listed page the role can open needs it (its gates, parent included);
 *   - it extends one of those on a page that is not view-only (`implies`, e.g. devices.manage on Devices);
 *   - a listed page shows it read-only (`uses`, dashboard widgets);
 *   - a kept permission depends on it (`implies`, e.g. devices.view under devices.manage).
 * Everything else is withdrawn, so the API of a hidden page is refused. Full administration is never limited.
 */
export function restrictPermissions(
  permissions: Iterable<string>,
  pages: PageSet,
  entries: readonly PageEntry[],
  implies: Readonly<Record<string, readonly string[]>>,
): string[] {
  const perms = [...permissions]
  if (!pages || perms.includes('system.admin')) return perms
  const granted = new Set(perms)
  const can: Can = (p) => granted.has(p)
  const keep = new Set<string>()
  const extendable = new Set<string>()
  for (const e of entries) {
    if (e.personal || !pages.has(e.key) || !pageReachable(e, can)) continue
    for (const g of e.gates) {
      for (const p of [...(g.all || []), ...(g.any || [])]) {
        if (!granted.has(p)) continue
        keep.add(p)
        if (!e.view_only) extendable.add(p)
      }
    }
    for (const p of e.uses || []) if (granted.has(p)) keep.add(p)
  }
  for (const p of perms) if ((implies[p] || []).some((q) => extendable.has(q))) keep.add(p)
  for (const p of [...keep]) for (const q of implies[p] || []) if (granted.has(q)) keep.add(q)
  return perms.filter((p) => keep.has(p))
}
