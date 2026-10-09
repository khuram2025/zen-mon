import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  filterValueLabel, normaliseAssetFilters, patchAssetFilters, rejectedText, type AssetFilterKey, type AssetFilters,
} from '@/pages/inventory/filters'
import { useAuth } from '@/stores/auth'
import { fmtInt, sortFacetItems, visibleFacetItems, type FacetItem } from './model'

/** A patch for `patchAssetFilters`: the change one click makes to the filters. */
export type FilterPatch = Parameters<typeof patchAssetFilters>[1]

export interface FacetGroupDef {
  /** The filter this group sets. */
  key: AssetFilterKey
  label: string
  items: readonly FacetItem[]
  /** Only `tag` is repeatable; every other group holds one value at a time. */
  multi?: boolean
  /** How the unknown row is addressed (for example `UNKNOWN_FILTER.vendor`). It is never a free string. */
  unknownPatch?: FilterPatch
  defaultOpen?: boolean
  /** Items listed before "Show more" (default 8). */
  limit?: number
  /** List the items in the order given (a fixed vocabulary such as fresh, aging, stale, never) instead of largest first. */
  keepOrder?: boolean
  /** Values the API holds beyond the ones it listed (its `distinct` minus the items): shown as a hint, never silently dropped. */
  more?: number
}

const STORAGE_KEY = 'zp-inventory-facets'

/** Which groups are open, per user (review finding FE-14): two people on one browser profile keep their own panel. */
export const facetStorageKey = (userId: string | null | undefined): string => `${STORAGE_KEY}:${userId ?? 'anonymous'}`

function readOpen(key: string): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(key)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeOpen(key: string, state: Record<string, boolean>) {
  try {
    localStorage.setItem(key, JSON.stringify(state))
  } catch {
    // Private window or blocked storage: the panel still works, it just forgets.
  }
}

/** What clicking an item changes, and whether it is selected right now. Pure so it can be tested. */
export function facetItemState(group: FacetGroupDef, item: FacetItem, filters: AssetFilters): { patch: FilterPatch; selected: boolean } {
  if (item.unknown && group.unknownPatch) {
    const patch = group.unknownPatch
    const selected = Object.entries(patch).every(([k, v]) => (filters as Record<string, unknown>)[k] === v)
    // Selected: clicking again removes exactly what the unknown row set.
    return { selected, patch: selected ? Object.fromEntries(Object.keys(patch).map((k) => [k, null])) as FilterPatch : patch }
  }
  if (group.multi) {
    const current = (filters[group.key] as string[] | undefined) ?? []
    const selected = current.some((v) => v.toLowerCase() === item.value.toLowerCase())
    const next = selected ? current.filter((v) => v.toLowerCase() !== item.value.toLowerCase()) : [...current, item.value]
    return { selected, patch: { [group.key]: next } as FilterPatch }
  }
  const current = filters[group.key] as string | undefined
  const selected = current !== undefined && current.toLowerCase() === item.value.toLowerCase()
  return { selected, patch: { [group.key]: selected ? null : item.value } as FilterPatch }
}

/**
 * Why a row cannot be opened, or null when it can. A value the API listed but cannot be sent back unchanged (white space
 * at an end that the API drops, a control character, over its length limit) would select nothing, or, with the value
 * left out, everything: neither is the list behind the row's count, so the row is shown as text with the reason (the
 * facet twin of DrillLink's unavailable number; review findings NF-FE-6, NB-05).
 */
export function facetItemUnavailable(group: FacetGroupDef, item: FacetItem): string | null {
  if (item.unknown && group.unknownPatch) return null
  const { rejected } = normaliseAssetFilters({ [group.key]: group.multi ? [item.value] : item.value } as AssetFilters)
  return rejected.length > 0 ? `No list can be opened for this value: ${rejected.map(rejectedText).join('; ')}.` : null
}

/**
 * The group's items plus the values the filters select that the API did not list. The API counts a facet over every
 * filter EXCEPT its own and omits rows with no match, so a selection that matches nothing (a deep link, a stale
 * bookmark, two filters that exclude each other) has no row; without one the person cannot see or untick what they
 * selected from the pane (review finding UI-09). Such a value is listed with a count of 0 and is selected.
 */
export function withSelectedValues(group: FacetGroupDef, filters: AssetFilters): FacetItem[] {
  const raw = filters[group.key]
  const wanted = raw === undefined ? [] : Array.isArray(raw) ? raw : [raw]
  const items = [...group.items]
  for (const value of wanted) {
    const lower = value.toLowerCase()
    const unknown = lower === 'unknown' && group.unknownPatch !== undefined
    const listed = items.some((i) => (unknown ? !!i.unknown : !i.unknown && i.value.toLowerCase() === lower))
    if (listed) continue
    items.push(unknown ? { value, label: 'Unknown', count: 0, unknown: true } : { value, label: filterValueLabel(group.key, value), count: 0 })
  }
  return items
}

export function selectedCount(group: FacetGroupDef, filters: AssetFilters): number {
  return withSelectedValues(group, filters).filter((i) => facetItemState(group, i, filters).selected).length
}

function FacetGroup({
  group, filters, hrefFor, open, onToggle,
}: {
  group: FacetGroupDef
  filters: AssetFilters
  hrefFor: (patch: FilterPatch) => string
  open: boolean
  onToggle: () => void
}) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const [needle, setNeedle] = useState('')
  const all = withSelectedValues(group, filters)
  const items = group.keepOrder ? all : sortFacetItems(all)
  const states = new Map(items.map((i) => [i, facetItemState(group, i, filters)]))
  const selected = items.filter((i) => states.get(i)!.selected)
  const { shown, hidden } = visibleFacetItems(items, selected.map((i) => i.value), { limit: group.limit, expanded, filter: needle })
  const searchable = items.length > 12

  return (
    <section data-facet={group.key} className="border-b border-border/70 last:border-b-0">
      <h2>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={id}
          className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary"
        >
          <ChevronDown aria-hidden className={cn('h-3.5 w-3.5 shrink-0 transition-transform', !open && '-rotate-90')} />
          <span className="min-w-0 flex-1 truncate">{group.label}</span>
          {!open && selected.length > 0 && (
            <span className="rounded-full bg-primary/15 px-1.5 text-[10px] font-bold tabular-nums text-primary" aria-label={`${selected.length} selected`}>
              {selected.length}
            </span>
          )}
        </button>
      </h2>
      <div id={id} hidden={!open} className="px-1.5 pb-2">
        {items.length === 0 ? (
          <p className="px-2 py-1 text-xs text-muted">No values in this selection</p>
        ) : (
          <>
            {searchable && (
              <input
                type="search"
                value={needle}
                onChange={(e) => setNeedle(e.target.value)}
                placeholder={`Filter ${group.label.toLowerCase()}…`}
                aria-label={`Filter ${group.label} values`}
                className="mx-1 mb-1 h-7 w-[calc(100%-0.5rem)] rounded-md border border-border bg-bg px-2 text-xs placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-primary/40"
              />
            )}
            <ul role="list" className="space-y-px">
              {shown.map((item) => {
                const { patch, selected: isOn } = states.get(item) ?? facetItemState(group, item, filters)
                const unavailable = isOn ? null : facetItemUnavailable(group, item)
                const rowClass = cn(
                  'group flex items-center gap-2 rounded-md px-2 py-1 text-[13px] outline-none transition-colors',
                  isOn ? 'bg-primary/10 font-semibold text-text' : 'text-text2',
                  item.unknown && !isOn && 'text-muted',
                )
                const inner = (
                  <>
                    <span
                      aria-hidden
                      className={cn(
                        'flex h-3.5 w-3.5 shrink-0 items-center justify-center border',
                        group.multi ? 'rounded-[3px]' : 'rounded-full',
                        isOn ? 'border-primary bg-primary text-on-primary' : 'border-border-strong bg-bg group-hover:border-primary/60',
                        unavailable && 'opacity-50',
                      )}
                    >
                      {isOn && <Check className="h-2.5 w-2.5" strokeWidth={3.5} />}
                    </span>
                    <span className={cn('min-w-0 flex-1 truncate', item.unknown && 'italic')}>{item.label ?? item.value}</span>
                    <span className="shrink-0 text-xs tabular-nums text-muted">{fmtInt(item.count)}</span>
                  </>
                )
                return (
                  <li key={`${item.unknown ? 'unknown:' : ''}${item.value}`}>
                    {unavailable ? (
                      <span title={unavailable} data-drill-unavailable="" data-drill-count={item.count} className={cn(rowClass, 'cursor-not-allowed')}>
                        {inner}
                        <span className="sr-only">({unavailable})</span>
                      </span>
                    ) : (
                      <Link
                        to={hrefFor(patch)}
                        aria-current={isOn ? 'true' : undefined}
                        data-drill-count={isOn ? undefined : item.count}
                        className={cn(rowClass, 'hover:bg-surface2 focus-visible:ring-2 focus-visible:ring-primary')}
                      >
                        {inner}
                      </Link>
                    )}
                  </li>
                )
              })}
            </ul>
            {needle && shown.length === 0 && <p className="px-2 py-1 text-xs text-muted">No match</p>}
            {(group.more ?? 0) > 0 && !needle && (
              <p className="px-2 py-1 text-[11px] text-muted">{fmtInt(group.more)} more values not listed. Narrow the other filters to see them.</p>
            )}
            {(hidden > 0 || (expanded && items.length > (group.limit ?? 8))) && !needle && (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
                className="mt-0.5 rounded px-2 py-1 text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
              >
                {expanded ? 'Show fewer' : `Show ${fmtInt(hidden)} more`}
              </button>
            )}
          </>
        )}
      </div>
    </section>
  )
}

/**
 * Collapsible facet groups with counts, bound to the URL. Every value is a link to the same page with
 * that filter toggled (`hrefFor`), so a click is a history entry, a facet can be opened in a new tab,
 * and the state is exactly what the address bar says. Selection is shown by a filled box and bold text
 * as well as the tint. The unknown row is addressed through the group's `unknownPatch`.
 */
export function FacetPanel({
  groups, filters, hrefFor, className, label = 'Filters',
}: {
  groups: readonly FacetGroupDef[]
  filters: AssetFilters
  /** URL of the current page with the patch applied (`useInventoryUrlState().hrefWith`). */
  hrefFor: (patch: FilterPatch) => string
  className?: string
  label?: string
}) {
  const userId = useAuth((s) => s.user?.id)
  const storageKey = facetStorageKey(userId)
  const [stored, setStored] = useState<{ key: string; open: Record<string, boolean> }>(() => ({ key: storageKey, open: readOpen(storageKey) }))
  // Another user signed in without a reload: read that user's layout.
  const open = stored.key === storageKey ? stored.open : readOpen(storageKey)
  const isOpen = (g: FacetGroupDef) => open[g.key] ?? g.defaultOpen ?? true
  const toggle = (g: FacetGroupDef) => {
    const next = { ...open, [g.key]: !isOpen(g) }
    writeOpen(storageKey, next)
    setStored({ key: storageKey, open: next })
  }
  return (
    <nav aria-label={label} className={cn('rounded-lg border border-border bg-surface shadow-card dark:shadow-card-dark', className)}>
      {groups.map((g) => (
        <FacetGroup key={g.key} group={g} filters={filters} hrefFor={hrefFor} open={isOpen(g)} onToggle={() => toggle(g)} />
      ))}
    </nav>
  )
}
