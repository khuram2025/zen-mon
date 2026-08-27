import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Waypoints,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { hasPermission, useAuth } from '@/stores/auth'
import {
  NAV_GROUPS,
  isBranchActive,
  isNodeActive,
  groupForLocation,
  type NavGroup,
  type NavNode,
} from './navigation'

export const SIDEBAR_RAIL = 76
export const SIDEBAR_WIDE = 256

/* ------------------------------------------------------------------ */
/*  Shared bits                                                        */
/* ------------------------------------------------------------------ */

function Brand({ compact }: { compact?: boolean }) {
  return (
    <div className={cn('flex items-center gap-2.5 overflow-hidden', compact && 'justify-center')}>
      <div className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-gradient-to-br from-primary to-accent shadow-[0_2px_10px_-2px_rgb(var(--primary)/0.6)]">
        <Waypoints className="h-[18px] w-[18px] text-white" />
      </div>
      {!compact && (
        <div className="min-w-0 leading-none">
          <div className="truncate text-[15px] font-bold tracking-tight text-sidebar-text">ZenPlus</div>
          <div className="mt-1 truncate text-[9.5px] font-medium uppercase tracking-[0.14em] text-sidebar-text-muted">
            Network Operations
          </div>
        </div>
      )}
    </div>
  )
}

function AlertBadge({ count }: { count: number }) {
  if (!count) return null
  return (
    <span className="ml-auto shrink-0 rounded-full bg-danger/15 px-1.5 py-px text-[10px] font-bold leading-4 text-danger">
      {count > 99 ? '99+' : count}
    </span>
  )
}

/** Filters out nodes the signed-in role may not see, children included. */
function usePermittedGroups(): NavGroup[] {
  const user = useAuth((s) => s.user)
  return useMemo(() => {
    const keep = (n: NavNode): NavNode | null => {
      if (n.permission && !hasPermission(user, n.permission)) return null
      const children = (n.children || []).map(keep).filter(Boolean) as NavNode[]
      if (n.children && children.length === 0) return null
      return n.children ? { ...n, children } : n
    }
    return NAV_GROUPS.map((g) => ({ ...g, items: g.items.map(keep).filter(Boolean) as NavNode[] })).filter(
      (g) => g.items.length > 0,
    )
  }, [user])
}

/* ------------------------------------------------------------------ */
/*  Rows                                                               */
/* ------------------------------------------------------------------ */

type RowProps = {
  node: NavNode
  pathname: string
  params: URLSearchParams
  alertCount: number
  onNavigate?: () => void
  depth?: number
}

function LeafRow({ node, pathname, params, alertCount, onNavigate, depth = 0 }: RowProps) {
  const active = isNodeActive(node, pathname, params)
  return (
    <Link
      to={node.to}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'group relative flex items-center gap-2.5 rounded-lg py-[7px] pr-2 text-[13px] transition-colors',
        depth === 0 ? 'px-2.5 font-medium' : 'px-2 text-[12.5px]',
        active
          ? 'bg-primary/[0.13] font-semibold text-primary'
          : 'text-sidebar-text/70 hover:bg-sidebar-hover hover:text-sidebar-text',
      )}
    >
      {active && depth === 0 && (
        <span className="absolute -left-1.5 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-primary" />
      )}
      {active && depth > 0 && (
        <span className="absolute -left-[13px] top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-primary ring-2 ring-sidebar-bg" />
      )}
      <node.icon className={cn('shrink-0', depth === 0 ? 'h-[17px] w-[17px]' : 'h-[15px] w-[15px]')} />
      <span className="truncate">{node.label}</span>
      {node.badge === 'alerts' && <AlertBadge count={alertCount} />}
    </Link>
  )
}

function BranchRow({
  node,
  pathname,
  params,
  alertCount,
  onNavigate,
  open,
  onToggle,
}: RowProps & { open: boolean; onToggle: () => void }) {
  const navigate = useNavigate()
  const branchActive = isBranchActive(node, pathname, params)

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          // A closed parent both opens and jumps to its landing page; an open
          // one just collapses, so the submenu never becomes a navigation trap.
          if (!open) {
            onToggle()
            navigate(node.to)
            onNavigate?.()
          } else {
            onToggle()
          }
        }}
        className={cn(
          'group relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-left text-[13px] font-medium transition-colors',
          branchActive
            ? 'text-sidebar-text'
            : 'text-sidebar-text/70 hover:bg-sidebar-hover hover:text-sidebar-text',
          branchActive && !open && 'bg-sidebar-hover',
        )}
      >
        {branchActive && (
          <span className="absolute -left-1.5 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-primary/70" />
        )}
        <node.icon className={cn('h-[17px] w-[17px] shrink-0', branchActive && 'text-primary')} />
        <span className="truncate">{node.label}</span>
        <ChevronDown
          className={cn(
            'ml-auto h-3.5 w-3.5 shrink-0 text-sidebar-text-muted transition-transform duration-200',
            open ? 'rotate-0' : '-rotate-90',
          )}
        />
      </button>

      <div className="nav-collapse" data-open={open}>
        <div>
          <div className="relative ml-[19px] mt-0.5 space-y-px border-l border-sidebar-border pl-3">
            {(node.children || []).map((child) => (
              <LeafRow
                key={child.to}
                node={child}
                pathname={pathname}
                params={params}
                alertCount={alertCount}
                onNavigate={onNavigate}
                depth={1}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Collapsed rail flyout                                              */
/* ------------------------------------------------------------------ */

function RailFlyout({
  group,
  top,
  pathname,
  params,
  alertCount,
  onClose,
  onEnter,
}: {
  group: NavGroup
  top: number
  pathname: string
  params: URLSearchParams
  alertCount: number
  onClose: () => void
  onEnter: () => void
}) {
  // Keep the panel on screen when a long group opens near the bottom.
  const rows = group.items.reduce((n, i) => n + 1 + (i.children?.length || 0), 0)
  // 56 covers the panel header plus the bottom gutter.
  const maxTop = Math.max(8, window.innerHeight - 56 - Math.min(rows * 30 + 48, window.innerHeight * 0.7))
  const clamped = Math.min(Math.max(top - 4, 8), maxTop)

  return (
    <div
      className="fixed z-50 pl-1.5"
      style={{ left: SIDEBAR_RAIL, top: clamped }}
      onMouseEnter={onEnter}
      onMouseLeave={onClose}
    >
      <div className="w-[248px] overflow-hidden rounded-xl border border-sidebar-border bg-sidebar-bg shadow-2xl animate-fade-in">
        <div className="flex items-center gap-2 border-b border-sidebar-border px-3 py-2">
          <group.icon className="h-4 w-4 text-primary" />
          <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-sidebar-text">
            {group.label}
          </span>
        </div>
        <div className="sidebar-scroll max-h-[70vh] overflow-y-auto p-1.5">
          {group.items.map((item) => {
            const branchActive = isBranchActive(item, pathname, params)
            return (
              <div key={item.to}>
                <NavLink
                  to={item.to}
                  onClick={onClose}
                  className={cn(
                    'flex items-start gap-2.5 rounded-lg px-2 py-1.5 transition-colors',
                    branchActive && !item.children
                      ? 'bg-primary/[0.13] text-primary'
                      : 'text-sidebar-text/80 hover:bg-sidebar-hover hover:text-sidebar-text',
                  )}
                >
                  <item.icon className={cn('mt-px h-[15px] w-[15px] shrink-0', branchActive && 'text-primary')} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-[12.5px] font-medium leading-tight">
                      <span className="truncate">{item.label}</span>
                      {item.badge === 'alerts' && <AlertBadge count={alertCount} />}
                    </span>
                    {item.hint && (
                      <span className="mt-0.5 block truncate text-[10.5px] leading-tight text-sidebar-text-muted">
                        {item.hint}
                      </span>
                    )}
                  </span>
                </NavLink>
                {item.children && (
                  <div className="ml-[18px] mb-1 border-l border-sidebar-border pl-2">
                    {item.children.map((child) => {
                      const active = isNodeActive(child, pathname, params)
                      return (
                        <Link
                          key={child.to}
                          to={child.to}
                          onClick={onClose}
                          aria-current={active ? 'page' : undefined}
                          className={cn(
                            'flex items-center gap-2 rounded-md px-2 py-1 text-[12px] transition-colors',
                            active
                              ? 'bg-primary/[0.13] font-semibold text-primary'
                              : 'text-sidebar-text/65 hover:bg-sidebar-hover hover:text-sidebar-text',
                          )}
                        >
                          <child.icon className="h-[13px] w-[13px] shrink-0" />
                          <span className="truncate">{child.label}</span>
                        </Link>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Sidebar                                                            */
/* ------------------------------------------------------------------ */

export function Sidebar({
  expanded,
  onToggleExpanded,
  variant = 'fixed',
  onNavigate,
}: {
  expanded: boolean
  onToggleExpanded: () => void
  /** `drawer` renders the wide sidebar inside the mobile overlay. */
  variant?: 'fixed' | 'drawer'
  onNavigate?: () => void
}) {
  const { pathname, search } = useLocation()
  const params = useMemo(() => new URLSearchParams(search), [search])
  const groups = usePermittedGroups()
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const wide = variant === 'drawer' || expanded

  const { data: stats } = useQuery<{ active?: number }>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })
  const alertCount = Number(stats?.active || 0)

  const { data: sysStatus } = useQuery<{ current_version?: string }>({
    queryKey: ['system-update-status'],
    queryFn: async () => (await api.get('/system/update-status')).data,
    retry: false,
  })

  /* --- group accordion ------------------------------------------- */
  const activeGroup = groupForLocation(pathname, params)
  const [openGroup, setOpenGroup] = useState<string | null>(() => {
    try { return localStorage.getItem('zp-nav-group') } catch { return null }
  })
  useEffect(() => {
    if (activeGroup) setOpenGroup(activeGroup.id)
  }, [activeGroup?.id])
  useEffect(() => {
    if (openGroup) localStorage.setItem('zp-nav-group', openGroup)
  }, [openGroup])

  /* --- submenu expansion ----------------------------------------- */
  // Explicit user toggles win; otherwise a parent opens when it owns the route.
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({})
  const isItemOpen = (item: NavNode) =>
    manualOpen[item.to] ?? isBranchActive(item, pathname, params)

  /* --- filter ----------------------------------------------------- */
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  const q = query.trim().toLowerCase()

  const filtered = useMemo(() => {
    if (!q) return null
    const out: { group: NavGroup; items: NavNode[] }[] = []
    for (const group of groups) {
      const items: NavNode[] = []
      for (const item of group.items) {
        const selfHit = item.label.toLowerCase().includes(q)
        const kids = (item.children || []).filter((c) => c.label.toLowerCase().includes(q))
        if (selfHit) items.push({ ...item, children: item.children })
        else if (kids.length) items.push({ ...item, children: kids })
      }
      if (items.length) out.push({ group, items })
    }
    return out
  }, [q, groups])

  // Once a filtered result is picked the nav returns to its normal tree.
  useEffect(() => { setQuery('') }, [pathname, search])

  // `/` focuses the nav filter from anywhere outside a text field.
  useEffect(() => {
    if (!wide) return
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null
      const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
      if (e.key === '/' && !typing) {
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [wide])

  /* --- collapsed rail flyout -------------------------------------- */
  const [flyout, setFlyout] = useState<{ group: NavGroup; top: number } | null>(null)
  const closeTimer = useRef<number | undefined>(undefined)
  const openFlyout = useCallback((group: NavGroup, el: HTMLElement) => {
    window.clearTimeout(closeTimer.current)
    setFlyout({ group, top: el.getBoundingClientRect().top })
  }, [])
  const scheduleClose = useCallback(() => {
    window.clearTimeout(closeTimer.current)
    closeTimer.current = window.setTimeout(() => setFlyout(null), 140)
  }, [])
  useEffect(() => () => window.clearTimeout(closeTimer.current), [])
  useEffect(() => { setFlyout(null) }, [pathname, search])

  /* --- render ------------------------------------------------------ */
  const railGroups = groups

  const nav = wide ? (
    <nav className="sidebar-scroll flex-1 space-y-1 overflow-y-auto overflow-x-hidden px-2.5 py-2">
      {(filtered ?? groups.map((g) => ({ group: g, items: g.items }))).map(({ group, items }) => {
        const open = !!filtered || openGroup === group.id
        const groupActive = activeGroup?.id === group.id
        return (
          <div key={group.id}>
            <button
              type="button"
              onClick={() => !filtered && setOpenGroup((cur) => (cur === group.id ? null : group.id))}
              aria-expanded={open}
              className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-sidebar-hover"
            >
              <span
                className={cn(
                  'text-[10px] font-bold uppercase tracking-[0.11em] transition-colors',
                  groupActive ? 'text-primary' : 'text-sidebar-text-muted',
                )}
              >
                {group.label}
              </span>
              {!open && groupActive && <span className="h-1 w-1 rounded-full bg-primary" />}
              <ChevronRight
                className={cn(
                  'ml-auto h-3 w-3 shrink-0 text-sidebar-text-muted transition-transform duration-200',
                  open && 'rotate-90',
                )}
              />
            </button>

            <div className="nav-collapse" data-open={open}>
              <div>
                <div className="ml-1.5 space-y-px pb-1 pl-1.5">
                  {items.map((item) =>
                    item.children && item.children.length > 0 ? (
                      <BranchRow
                        key={item.to}
                        node={item}
                        pathname={pathname}
                        params={params}
                        alertCount={alertCount}
                        onNavigate={onNavigate}
                        open={!!filtered || isItemOpen(item)}
                        onToggle={() =>
                          setManualOpen((m) => ({ ...m, [item.to]: !isItemOpen(item) }))
                        }
                      />
                    ) : (
                      <LeafRow
                        key={item.to}
                        node={item}
                        pathname={pathname}
                        params={params}
                        alertCount={alertCount}
                        onNavigate={onNavigate}
                      />
                    ),
                  )}
                </div>
              </div>
            </div>
          </div>
        )
      })}

      {filtered && filtered.length === 0 && (
        <div className="px-3 py-6 text-center text-xs text-sidebar-text-muted">
          No pages match “{query}”.
        </div>
      )}
    </nav>
  ) : (
    <nav className="sidebar-scroll flex-1 space-y-1 overflow-y-auto overflow-x-hidden px-2 py-2">
      {railGroups.map((group) => {
        const groupActive = activeGroup?.id === group.id
        return (
          <button
            key={group.id}
            type="button"
            aria-label={group.label}
            onMouseEnter={(e) => openFlyout(group, e.currentTarget)}
            onMouseLeave={scheduleClose}
            onFocus={(e) => openFlyout(group, e.currentTarget)}
            onClick={() => navigate(group.items[0].to)}
            className={cn(
              'relative flex w-full flex-col items-center gap-1 rounded-xl py-2 transition-colors',
              groupActive
                ? 'bg-primary/[0.13] text-primary'
                : 'text-sidebar-text/65 hover:bg-sidebar-hover hover:text-sidebar-text',
            )}
          >
            {groupActive && (
              <span className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-primary" />
            )}
            <span className="relative">
              <group.icon className="h-[19px] w-[19px]" />
              {group.id === 'alerting' && alertCount > 0 && (
                <span className="absolute -right-1.5 -top-1 min-w-[15px] rounded-full bg-danger px-1 text-center text-[9px] font-bold leading-[15px] text-white">
                  {alertCount > 99 ? '99+' : alertCount}
                </span>
              )}
            </span>
            <span className="max-w-full truncate text-[9px] font-semibold uppercase leading-none tracking-tight">
              {group.short}
            </span>
          </button>
        )
      })}
    </nav>
  )

  const body = (
    <>
      {/* Brand */}
      <div
        className={cn(
          'flex h-14 shrink-0 items-center border-b border-sidebar-border',
          wide ? 'gap-2 px-3.5' : 'justify-center px-2',
        )}
      >
        <Brand compact={!wide} />
        {variant === 'drawer' ? (
          <button
            type="button"
            onClick={onNavigate}
            aria-label="Close navigation"
            className="ml-auto flex h-7 w-7 items-center justify-center rounded-md text-sidebar-text-muted hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <X className="h-4 w-4" />
          </button>
        ) : wide ? (
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
            className="ml-auto flex h-7 w-7 items-center justify-center rounded-md text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <PanelLeftClose className="h-4 w-4" />
          </button>
        ) : null}
      </div>

      {/* Filter */}
      {wide && (
        <div className="shrink-0 px-3 pb-1 pt-2.5">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sidebar-text-muted" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') { setQuery(''); e.currentTarget.blur() }
                if (e.key === 'Enter') {
                  const first = filtered?.[0]?.items?.[0]
                  if (first) { navigate(first.to); setQuery(''); onNavigate?.() }
                }
              }}
              placeholder="Jump to…"
              aria-label="Filter navigation"
              className="h-8 w-full rounded-lg border border-sidebar-border bg-sidebar-hover/60 pl-8 pr-8 text-[12.5px] text-sidebar-text placeholder:text-sidebar-text-muted focus:border-primary/40 focus:outline-none focus:ring-1 focus:ring-primary/30"
            />
            {query ? (
              <button
                type="button"
                onClick={() => setQuery('')}
                aria-label="Clear filter"
                className="absolute right-1.5 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded text-sidebar-text-muted hover:text-sidebar-text"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : (
              <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-sidebar-border px-1 text-[9px] font-semibold text-sidebar-text-muted">
                /
              </kbd>
            )}
          </div>
        </div>
      )}

      {nav}

      {/* Footer */}
      <div className={cn('shrink-0 border-t border-sidebar-border', wide ? 'p-2.5' : 'p-2')}>
        {wide ? (
          <div className="flex items-center gap-2.5 rounded-lg px-1.5 py-1.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[12px] font-bold text-primary">
              {(user?.username || 'U')[0].toUpperCase()}
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-[12.5px] font-semibold text-sidebar-text">
                {user?.full_name || user?.username}
              </div>
              <div className="truncate text-[10px] uppercase tracking-wide text-sidebar-text-muted">
                {user?.role || 'user'}
                {sysStatus?.current_version ? ` · v${sysStatus.current_version}` : ''}
              </div>
            </div>
            <button
              type="button"
              onClick={() => { logout(); navigate('/login') }}
              title="Sign out"
              aria-label="Sign out"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sidebar-text-muted transition-colors hover:bg-danger/10 hover:text-danger"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            className="flex w-full items-center justify-center rounded-lg py-2 text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        )}
      </div>
    </>
  )

  if (variant === 'drawer') {
    return (
      <aside
        className="flex h-full w-[272px] flex-col border-r border-sidebar-border bg-sidebar-bg"
        aria-label="Main navigation"
      >
        {body}
      </aside>
    )
  }

  return (
    <>
      <aside
        className="sidebar-transition fixed inset-y-0 left-0 z-30 hidden flex-col border-r border-sidebar-border bg-sidebar-bg md:flex"
        style={{ width: wide ? SIDEBAR_WIDE : SIDEBAR_RAIL }}
        aria-label="Main navigation"
      >
        {body}
      </aside>
      {!wide && flyout && (
        <RailFlyout
          group={flyout.group}
          top={flyout.top}
          pathname={pathname}
          params={params}
          alertCount={alertCount}
          onEnter={() => window.clearTimeout(closeTimer.current)}
          onClose={scheduleClose}
        />
      )}
    </>
  )
}
