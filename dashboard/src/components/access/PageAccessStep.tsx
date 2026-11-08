import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AppWindow, Lock, Map as MapIcon, PanelsTopLeft, Search, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/Input'
import { NAV_GROUPS, type NavNode } from '@/components/layout/navigation'
import { canFromPermissions, gatePermissions, pageCatalog, pageReachable } from '@/components/layout/navAccess'
import type { ManualMapListItem } from '@/features/maps/core'
import { branchState, toggleBranch } from './roleModel'

/** Every page of the sidebar with the permissions it needs; computed once (the tree is static). */
export const PAGE_ENTRIES = pageCatalog(NAV_GROUPS)
const ENTRY_BY_KEY = new Map(PAGE_ENTRIES.map((e) => [e.key, e]))

type Leaf = { node: NavNode; parent?: NavNode }

function leavesOf(item: NavNode): Leaf[] {
  return item.children?.length ? item.children.map((c) => ({ node: c, parent: item })) : [{ node: item }]
}

/** Pages the permissions open (the keys a role limited to pages can usefully list). */
export function reachablePages(granted: Iterable<string>): Set<string> {
  const can = canFromPermissions(granted)
  return new Set(PAGE_ENTRIES.filter((e) => !e.personal && pageReachable(e, can)).map((e) => e.key))
}

function Check({ state, disabled, onChange, label }: { state: 'all' | 'some' | 'none'; disabled?: boolean; onChange: () => void; label: string }) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = state === 'some' }, [state])
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      className="h-3.5 w-3.5 shrink-0 accent-primary disabled:opacity-40"
      checked={state === 'all'}
      disabled={disabled}
      onChange={onChange}
    />
  )
}

function Choice({ checked, onClick, title, detail, icon: Icon }: { checked: boolean; onClick: () => void; title: string; detail: string; icon: typeof AppWindow }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      onClick={onClick}
      className={cn(
        'flex items-start gap-3 rounded-lg border p-3 text-left transition-colors',
        checked ? 'border-primary bg-primary/[0.06] ring-1 ring-primary/40' : 'border-border hover:border-primary/40',
      )}
    >
      <span className={cn('mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border', checked ? 'border-primary' : 'border-border-strong')}>
        {checked && <span className="h-2 w-2 rounded-full bg-primary" />}
      </span>
      <span className="min-w-0">
        <span className="flex items-center gap-1.5 text-sm font-medium"><Icon className="h-3.5 w-3.5 text-primary" aria-hidden />{title}</span>
        <span className="mt-0.5 block text-xs text-muted">{detail}</span>
      </span>
    </button>
  )
}

/**
 * Role editor step "Pages": optionally limit members to selected sidebar pages (a checkbox tree grouped like the
 * sidebar) and to selected network maps (the NOC wall views). Pages the role's permissions do not open are shown
 * locked with the permission they need. The server narrows the role's permissions to what the listed pages need,
 * so the API of an unlisted page is refused as well.
 */
export function PageAccessStep({
  granted, full, limitPages, onLimitPages, pages, onPages, limitMaps, onLimitMaps, maps, onMaps, removedPermissions, canListMaps, errors,
}: {
  granted: ReadonlySet<string>
  full: boolean
  limitPages: boolean
  onLimitPages: (v: boolean) => void
  pages: ReadonlySet<string>
  onPages: (next: Set<string>) => void
  limitMaps: boolean
  onLimitMaps: (v: boolean) => void
  maps: ReadonlySet<string>
  onMaps: (next: Set<string>) => void
  /** Permissions the page list withdraws (for the explanation under the tree). */
  removedPermissions: string[]
  /** The signed-in account may list network maps (maps.view). */
  canListMaps: boolean
  errors: { pages?: string; maps?: string }
}) {
  const [query, setQuery] = useState('')
  const can = useMemo(() => canFromPermissions(granted), [granted])
  const reachable = (key: string) => {
    const e = ENTRY_BY_KEY.get(key)
    return !!e && (e.personal || pageReachable(e, can))
  }

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    return NAV_GROUPS.map((g) => ({
      group: g,
      items: g.items
        .map((item) => ({ item, leaves: leavesOf(item).filter((l) => !l.node.personal) }))
        .filter(({ item, leaves }) => leaves.length > 0 && (!q || `${g.label} ${item.label} ${leaves.map((l) => l.node.label).join(' ')}`.toLowerCase().includes(q))),
    })).filter((g) => g.items.length > 0)
  }, [query])

  const mapsQuery = useQuery<{ data: ManualMapListItem[] }>({
    queryKey: ['manual-maps'],
    queryFn: async () => (await api.get('/maps')).data,
    enabled: canListMaps && limitMaps,
    staleTime: 30_000,
  })
  const mapList = mapsQuery.data?.data || []
  const unknownMaps = [...maps].filter((id) => !mapList.some((m) => m.id.toLowerCase() === id.toLowerCase()))

  if (full) {
    return (
      <div className="space-y-3">
        <Heading />
        <p role="note" className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
          Full administration always opens every page and network map, so it cannot be limited to selected pages. Remove it under Access to build a page-limited role.
        </p>
      </div>
    )
  }

  const toggleKeys = (keys: string[]) => onPages(toggleBranch(pages, keys.filter(reachable)))

  return (
    <div className="space-y-5">
      <Heading />
      <section className="space-y-3" aria-label="Pages">
        <div role="radiogroup" aria-label="Pages members can open" className="grid gap-2 md:grid-cols-2">
          <Choice checked={!limitPages} onClick={() => onLimitPages(false)} icon={AppWindow} title="Every page the permissions allow" detail="Members see every page their permissions open. This is how every role works unless you limit it." />
          <Choice checked={limitPages} onClick={() => onLimitPages(true)} icon={PanelsTopLeft} title="Only the pages I pick" detail="For helpdesk teams and NOC wall screens. Unlisted pages are hidden, and their data is refused by the appliance too." />
        </div>

        {limitPages && (
          <div className="space-y-2 rounded-lg border border-border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[12rem] flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
                <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a page…" className="h-8 pl-8 text-xs" aria-label="Find a page" />
              </div>
              <span className="text-xs text-muted tabular-nums">{[...pages].filter(reachable).length} selected</span>
            </div>
            <p className={cn('text-xs', errors.pages ? 'text-danger' : 'text-muted')}>
              {errors.pages || 'Members land on the first ticked page. Appearance, Support and My Profile are always available.'}
            </p>
            <div className="max-h-[26rem] space-y-3 overflow-y-auto pr-1">
              {groups.map(({ group, items }) => {
                const groupKeys = items.flatMap(({ leaves }) => leaves.map((l) => l.node.to)).filter(reachable)
                return (
                  <fieldset key={group.id} className="rounded-md border border-border">
                    <legend className="sr-only">{group.label}</legend>
                    <label className="flex items-center gap-2 border-b border-border bg-surface2/40 px-2.5 py-1.5">
                      <Check state={branchState(pages, groupKeys)} disabled={groupKeys.length === 0} onChange={() => toggleKeys(groupKeys)} label={`All of ${group.label}`} />
                      <group.icon className="h-3.5 w-3.5 text-primary" aria-hidden />
                      <span className="text-xs font-semibold">{group.label}</span>
                      {groupKeys.length === 0 && <span className="ml-auto text-2xs text-muted">Not open to these permissions</span>}
                    </label>
                    <ul className="divide-y divide-border">
                      {items.map(({ item, leaves }) => {
                        const nested = !!item.children?.length
                        const keys = leaves.map((l) => l.node.to).filter(reachable)
                        return (
                          <li key={item.to} className="px-2.5 py-1.5">
                            {nested ? (
                              <>
                                <label className="flex items-center gap-2">
                                  <Check state={branchState(pages, keys)} disabled={keys.length === 0} onChange={() => toggleKeys(keys)} label={`All of ${item.label}`} />
                                  <item.icon className="h-3.5 w-3.5 text-muted" aria-hidden />
                                  <span className="text-xs font-medium">{item.label}</span>
                                  {keys.length === 0 && <NeedsHint node={item} />}
                                </label>
                                <ul className="ml-6 mt-1 grid gap-x-3 gap-y-1 sm:grid-cols-2">
                                  {leaves.map(({ node }) => <LeafRow key={node.to} node={node} on={pages.has(node.to)} open={reachable(node.to)} onToggle={() => toggleKeys([node.to])} parent={item} />)}
                                </ul>
                              </>
                            ) : (
                              <LeafRow node={item} on={pages.has(item.to)} open={reachable(item.to)} onToggle={() => toggleKeys([item.to])} />
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  </fieldset>
                )
              })}
              {groups.length === 0 && <p className="text-xs text-muted">No page matches “{query}”.</p>}
            </div>
            {removedPermissions.length > 0 && (
              <p className="rounded-md bg-surface2/50 px-2.5 py-2 text-xs text-text2">
                Members will not use these permissions, because no ticked page needs them:{' '}
                {removedPermissions.map((p) => <code key={p} className="mr-1 rounded bg-surface px-1 py-0.5 font-mono text-2xs">{p}</code>)}
              </p>
            )}
          </div>
        )}
      </section>

      <section className="space-y-3" aria-label="Network maps">
        <div>
          <h4 className="flex items-center gap-1.5 text-sm font-semibold"><MapIcon className="h-3.5 w-3.5 text-primary" aria-hidden /> Network maps</h4>
          <p className="text-xs text-muted">Network maps are the live views shown on NOC wall screens. Limit members to specific maps; others are hidden from the map list and cannot be opened.</p>
        </div>
        <div role="radiogroup" aria-label="Network maps members can open" className="grid gap-2 md:grid-cols-2">
          <Choice checked={!limitMaps} onClick={() => onLimitMaps(false)} icon={MapIcon} title="Every map" detail="Members with access to Network Maps see every map." />
          <Choice checked={limitMaps} onClick={() => onLimitMaps(true)} icon={Lock} title="Only the maps I pick" detail="Members cannot create maps while this is on." />
        </div>
        {limitMaps && (
          <div className="space-y-2 rounded-lg border border-border p-3">
            {!canListMaps && <p className="text-xs text-warning">Your role cannot list network maps, so the current selection is kept as it is.</p>}
            {canListMaps && mapsQuery.isLoading && <p className="text-xs text-muted">Loading maps…</p>}
            {canListMaps && mapsQuery.isError && <p className="text-xs text-danger">Could not load the network maps.</p>}
            {canListMaps && mapsQuery.isSuccess && mapList.length === 0 && <p className="text-xs text-muted">No network maps exist yet. Create one under Network → Network Maps first.</p>}
            {mapList.length > 0 && (
              <ul className="grid max-h-60 gap-1 overflow-y-auto sm:grid-cols-2">
                {mapList.map((m) => {
                  const on = [...maps].some((id) => id.toLowerCase() === m.id.toLowerCase())
                  return (
                    <li key={m.id}>
                      <label className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-surface2/50">
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 accent-primary"
                          checked={on}
                          onChange={() => {
                            const next = new Set([...maps].filter((id) => id.toLowerCase() !== m.id.toLowerCase()))
                            if (!on) next.add(m.id.toLowerCase())
                            onMaps(next)
                          }}
                        />
                        <span className="min-w-0 truncate text-xs">{m.name}</span>
                        <span className="ml-auto shrink-0 text-2xs text-muted tabular-nums">{m.node_count} nodes</span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            )}
            {mapsQuery.isSuccess && unknownMaps.length > 0 && (
              <p className="text-xs text-warning">
                {unknownMaps.length} selected {unknownMaps.length === 1 ? 'map no longer exists' : 'maps no longer exist'}.{' '}
                <button type="button" className="underline" onClick={() => onMaps(new Set([...maps].filter((id) => !unknownMaps.includes(id))))}>Remove</button>
              </p>
            )}
            {errors.maps && <p className="text-xs text-danger">{errors.maps}</p>}
          </div>
        )}
      </section>
    </div>
  )
}

function Heading() {
  return (
    <div>
      <h3 className="text-sm font-semibold">Which pages can members open?</h3>
      <p className="text-xs text-muted">Permissions decide what members may do; a page list narrows them to specific pages and dashboards, for example a helpdesk that only needs Devices and Active Alerts, or a wall screen that shows one dashboard.</p>
    </div>
  )
}

function NeedsHint({ node, parent }: { node: NavNode; parent?: NavNode }) {
  const needed = [...new Set([...(parent ? gatePermissions(parent) : []), ...gatePermissions(node)])]
  return <span className="ml-auto truncate text-2xs text-muted" title={`Needs ${needed.join(' or ')}`}>Needs {needed.join(' / ')}</span>
}

function LeafRow({ node, parent, on, open, onToggle }: { node: NavNode; parent?: NavNode; on: boolean; open: boolean; onToggle: () => void }) {
  return (
    <label className={cn('flex min-w-0 items-center gap-2', !open && 'opacity-60')}>
      <input type="checkbox" className="h-3.5 w-3.5 shrink-0 accent-primary" checked={on && open} disabled={!open} onChange={onToggle} />
      {!parent && <node.icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
      <span className="truncate text-xs">{node.label}</span>
      {!open && <NeedsHint node={node} parent={parent} />}
    </label>
  )
}
