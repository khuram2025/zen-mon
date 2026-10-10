import type { NavGroup, NavNode } from './navigation'

/**
 * Groups that are ONE feature with several tabs behind one permission. A role that lacks the permission lacks every
 * tab, so the denied screen names the module: "Overview" alone reads as the home dashboard (UI-23).
 */
const MODULE_GROUPS: ReadonlySet<string> = new Set(['inventory'])

/**
 * What the denied screen (RouteGate) calls the page the role may not open: the navigation entry's own label, or
 * the module's name for a page of a module group ("Inventory & Compliance"). `undefined` when the address has no
 * navigation entry (the screen then says "this page").
 */
export function deniedPageName(trail: { group: Pick<NavGroup, 'id' | 'label'>; item: Pick<NavNode, 'label'>; child?: Pick<NavNode, 'label'> } | null | undefined): string | undefined {
  if (!trail) return undefined
  if (MODULE_GROUPS.has(trail.group.id)) return trail.group.label
  return (trail.child ?? trail.item).label
}
