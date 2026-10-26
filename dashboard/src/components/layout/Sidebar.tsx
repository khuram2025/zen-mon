import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  CornerDownLeft,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Star,
  Waypoints,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { hasPermission, useAuth } from '@/stores/auth'
import {
  NAV_GROUPS,
  NAV_ZONES,
  isBranchActive,
  isNodeActive,
  groupForLocation,
  type NavGroup,
  type NavNode,
} from './navigation'
import { filterNavGroups } from './navAccess'

// In rem, so the sidebar grows with the display density along with its text (72px / 264px at the 15px default).
export const SIDEBAR_RAIL = '4.8rem'
export const SIDEBAR_WIDE = '17.6rem'

const STARS_KEY = 'zp-nav-stars'

/* ------------------------------------------------------------------ */
/*  Shared bits                                                        */
/* ------------------------------------------------------------------ */

function Brand({ compact }: { compact?: boolean }) {
  return (
    <div className={cn('flex min-w-0 items-center gap-2.5 overflow-hidden', compact && 'justify-center')}>
      <div className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-gradient-to-br from-primary to-accent shadow-[0_2px_10px_-2px_rgb(var(--primary)/0.6)]">
        <Waypoints className="h-[18px] w-[18px] text-white" />
      </div>
      {!compact && (
        <div className="min-w-0 leading-none">
          <div className="truncate text-base font-bold tracking-tight text-sidebar-text">ZenPlus</div>
          <div className="mt-1 truncate text-2xs font-medium uppercase tracking-[0.14em] text-sidebar-text-muted">
            Network Operations
          </div>
        </div>
      )}
    </div>
  )
}

function AlertBadge({ count, className }: { count: number; className?: string }) {
  if (!count) return null
  return (
    <span
      className={cn(
        'ml-auto shrink-0 rounded-full bg-danger px-1.5 text-center text-2xs font-bold leading-[18px] text-white',
        className,
      )}
    >
      {count > 99 ? '99+' : count}
    </span>
  )
}

function SectionLabel({ children, className }: { children: string; className?: string }) {
  return (
    <div
      className={cn(
        'select-none px-2.5 pb-1 pt-2.5 text-2xs font-semibold uppercase tracking-[0.12em] text-sidebar-text-muted',
        className,
      )}
    >
      {children}
    </div>
  )
}

/** Filters out nodes the signed-in role may not see, children included. */
export function usePermittedGroups(): NavGroup[] {
  const user = useAuth((s) => s.user)
  return useMemo(() => filterNavGroups(NAV_GROUPS, (p) => hasPermission(user, p)), [user])
}

/** Every page the role can open, with the path that leads to it (for search and starred pages). */
type Entry = { node: NavNode; group: NavGroup; parent?: NavNode }

function flatten(groups: NavGroup[]): Entry[] {
  const out: Entry[] = []
  for (const group of groups) {
    for (const item of group.items) {
      out.push({ node: item, group })
      for (const child of item.children || []) out.push({ node: child, group, parent: item })
    }
  }
  return out
}

function entryKey(e: Entry): string {
  // A parent and its first child can share a target (/services and All Checks); both are listed once.
  return e.parent ? `${e.parent.to}>${e.node.to}` : e.node.to
}

function entryTrail(e: Entry): string {
  return e.parent ? `${e.group.label} › ${e.parent.label}` : e.group.label
}

/** Pages a user starred, kept in this browser only. Unknown or no longer permitted pages are skipped on read. */
function useStars(): [string[], (to: string) => void] {
  const [stars, setStars] = useState<string[]>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(STARS_KEY) || '[]')
      return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string') : []
    } catch {
      return []
    }
  })
  const toggle = useCallback((to: string) => {
    setStars((cur) => {
      const next = cur.includes(to) ? cur.filter((x) => x !== to) : [...cur, to]
      try { localStorage.setItem(STARS_KEY, JSON.stringify(next)) } catch { /* private mode */ }
      return next
    })
  }, [])
  return [stars, toggle]
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
  /** 1 = a row inside a group, 2 = a submenu row. */
  depth?: 1 | 2
  starred?: boolean
  onToggleStar?: (to: string) => void
  /** Draw the node's own icon (starred list and search results; group rows keep the tree quiet). */
  showIcon?: boolean
  /** Muted second line, e.g. where a starred page lives. */
  caption?: string
}

function StarButton({ to, starred, label, onToggle }: { to: string; starred: boolean; label: string; onToggle: (to: string) => void }) {
  return (
    <button
      type="button"
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); onToggle(to) }}
      aria-label={starred ? `Remove ${label} from starred` : `Star ${label}`}
      aria-pressed={starred}
      title={starred ? 'Remove from starred' : 'Add to starred'}
      className={cn(
        'absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md transition-opacity',
        'text-sidebar-text-muted hover:bg-sidebar-bg hover:text-warning focus-visible:opacity-100',
        starred ? 'text-warning opacity-0 group-hover/row:opacity-100' : 'opacity-0 group-hover/row:opacity-100',
      )}
    >
      <Star className={cn('h-3.5 w-3.5', starred && 'fill-current')} />
    </button>
  )
}

function LeafRow({ node, pathname, params, alertCount, onNavigate, depth = 1, starred = false, onToggleStar, showIcon, caption }: RowProps) {
  const active = isNodeActive(node, pathname, params)
  const badge = node.badge === 'alerts' ? alertCount : 0
  return (
    <div className="group/row relative">
      <Link
        to={node.to}
        onClick={onNavigate}
        aria-current={active ? 'page' : undefined}
        title={node.hint}
        className={cn(
          'relative flex items-center gap-2.5 rounded-md pl-2.5 pr-2 transition-colors',
          depth === 1 ? 'min-h-[32px] py-1 text-sm' : 'min-h-[28px] py-0.5 text-sm',
          active
            ? 'bg-primary/10 font-semibold text-primary'
            : 'text-sidebar-text/75 hover:bg-sidebar-hover hover:text-sidebar-text',
          onToggleStar && 'pr-8',
        )}
      >
        {active && !showIcon && (
          <span className="absolute -left-[11px] top-1.5 bottom-1.5 w-[2px] rounded-full bg-primary" aria-hidden />
        )}
        {showIcon && <node.icon className={cn('h-4 w-4 shrink-0', active ? 'text-primary' : 'text-sidebar-text-muted')} />}
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate">{node.label}</span>
          {caption && <span className="block truncate text-xs font-normal text-sidebar-text-muted">{caption}</span>}
        </span>
        {badge > 0 && <AlertBadge count={badge} className={cn(onToggleStar && 'group-hover/row:invisible')} />}
      </Link>
      {onToggleStar && <StarButton to={node.to} starred={starred} label={node.label} onToggle={onToggleStar} />}
    </div>
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
  stars,
  onToggleStar,
}: Omit<RowProps, 'depth' | 'starred'> & { open: boolean; onToggle: () => void; stars: string[] }) {
  const navigate = useNavigate()
  const branchActive = isBranchActive(node, pathname, params)

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        title={node.hint}
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
          'relative flex min-h-[32px] w-full items-center gap-2.5 rounded-md py-1 pl-2.5 pr-2 text-left text-sm transition-colors',
          branchActive
            ? 'font-medium text-sidebar-text'
            : 'text-sidebar-text/75 hover:bg-sidebar-hover hover:text-sidebar-text',
          branchActive && !open && 'bg-primary/10 text-primary',
        )}
      >
        {branchActive && !open && (
          <span className="absolute -left-[11px] top-1.5 bottom-1.5 w-[2px] rounded-full bg-primary" aria-hidden />
        )}
        <span className="min-w-0 flex-1 truncate">{node.label}</span>
        <ChevronDown
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-sidebar-text-muted transition-transform duration-200',
            open ? 'rotate-0' : '-rotate-90',
          )}
        />
      </button>

      <div className="nav-collapse" data-open={open}>
        <div>
          <div className="relative mb-1 ml-2.5 mt-0.5 space-y-px border-l border-sidebar-border pl-2.5">
            {(node.children || []).map((child) => (
              <LeafRow
                key={child.to}
                node={child}
                pathname={pathname}
                params={params}
                alertCount={alertCount}
                onNavigate={onNavigate}
                depth={2}
                starred={stars.includes(child.to)}
                onToggleStar={onToggleStar}
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
  const rows = group.items.reduce((n, i) => n + 1 + (i.children?.length || 0) + (i.section ? 1 : 0), 0)
  // 56 covers the panel header plus the bottom gutter.
  const maxTop = Math.max(8, window.innerHeight - 56 - Math.min(rows * 30 + 48, window.innerHeight * 0.75))
  const clamped = Math.min(Math.max(top - 4, 8), maxTop)

  return (
    <div
      className="fixed z-50 pl-2"
      style={{ left: SIDEBAR_RAIL, top: clamped }}
      onMouseEnter={onEnter}
      onMouseLeave={onClose}
    >
      <div
        role="menu"
        aria-label={group.label}
        className="w-[17.6rem] overflow-hidden rounded-xl border border-sidebar-border bg-sidebar-bg shadow-2xl ring-1 ring-black/5 animate-fade-in"
      >
        <div className="flex items-center gap-2.5 border-b border-sidebar-border px-3 py-2.5">
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-primary/10 text-primary">
            <group.icon className="h-3.5 w-3.5" />
          </span>
          <span className="truncate text-sm font-semibold text-sidebar-text">{group.label}</span>
        </div>
        <div className="sidebar-scroll max-h-[75vh] overflow-y-auto p-1.5">
          {group.items.map((item) => {
            const branchActive = isBranchActive(item, pathname, params)
            const selfActive = !item.children && isNodeActive(item, pathname, params)
            return (
              <Fragment key={item.to}>
                {item.section && <SectionLabel className="px-2 pt-2">{item.section}</SectionLabel>}
                <Link
                  to={item.to}
                  role="menuitem"
                  onClick={onClose}
                  aria-current={selfActive ? 'page' : undefined}
                  className={cn(
                    'flex items-start gap-2.5 rounded-lg px-2 py-1.5 transition-colors',
                    selfActive
                      ? 'bg-primary/10 text-primary'
                      : 'text-sidebar-text/80 hover:bg-sidebar-hover hover:text-sidebar-text',
                  )}
                >
                  <item.icon className={cn('mt-px h-[15px] w-[15px] shrink-0', branchActive ? 'text-primary' : 'text-sidebar-text-muted')} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-sm font-medium leading-tight">
                      <span className="truncate">{item.label}</span>
                      {item.badge === 'alerts' && <AlertBadge count={alertCount} />}
                    </span>
                    {item.hint && (
                      <span className="mt-0.5 block truncate text-xs leading-tight text-sidebar-text-muted">
                        {item.hint}
                      </span>
                    )}
                  </span>
                </Link>
                {item.children && (
                  <div className="mb-1 ml-[17px] border-l border-sidebar-border pl-2">
                    {item.children.map((child) => {
                      const active = isNodeActive(child, pathname, params)
                      return (
                        <Link
                          key={child.to}
                          to={child.to}
                          role="menuitem"
                          onClick={onClose}
                          aria-current={active ? 'page' : undefined}
                          className={cn(
                            'flex items-center rounded-md px-2 py-1 text-sm transition-colors',
                            active
                              ? 'bg-primary/10 font-semibold text-primary'
                              : 'text-sidebar-text/70 hover:bg-sidebar-hover hover:text-sidebar-text',
                          )}
                        >
                          <span className="truncate">{child.label}</span>
                        </Link>
                      )
                    })}
                  </div>
                )}
              </Fragment>
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
  const entries = useMemo(() => flatten(groups), [groups])
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const wide = variant === 'drawer' || expanded

  const canSeeAlerts = hasPermission(user, 'alerts.view')
  const { data: stats } = useQuery<{ active?: number }>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
    enabled: canSeeAlerts,
  })
  const alertCount = canSeeAlerts ? Number(stats?.active || 0) : 0

  const { data: sysStatus } = useQuery<{ current_version?: string }>({
    queryKey: ['system-update-status'],
    queryFn: async () => (await api.get('/system/update-status')).data,
    retry: false,
    // Update status is a Full administration endpoint.
    enabled: hasPermission(user, 'system.admin'),
  })

  /* --- starred pages ---------------------------------------------- */
  const [stars, toggleStar] = useStars()
  const starred = useMemo(() => {
    const byTo = new Map<string, Entry>()
    for (const e of entries) if (!byTo.has(e.node.to)) byTo.set(e.node.to, e)
    return stars.map((to) => byTo.get(to)).filter(Boolean) as Entry[]
  }, [stars, entries])

  /* --- group accordion ------------------------------------------- */
  const activeGroup = groupForLocation(pathname, params)
  const [openGroup, setOpenGroup] = useState<string | null>(() => {
    try { return localStorage.getItem('zp-nav-group') } catch { return null }
  })
  useEffect(() => {
    if (activeGroup) setOpenGroup(activeGroup.id)
  }, [activeGroup?.id])
  useEffect(() => {
    try { if (openGroup) localStorage.setItem('zp-nav-group', openGroup) } catch { /* private mode */ }
  }, [openGroup])

  /* --- submenu expansion ----------------------------------------- */
  // Explicit user toggles win; otherwise a parent opens when it owns the route.
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({})
  const isItemOpen = (item: NavNode) =>
    manualOpen[item.to] ?? isBranchActive(item, pathname, params)

  /* --- search ------------------------------------------------------ */
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const searchRef = useRef<HTMLInputElement>(null)
  const q = query.trim().toLowerCase()

  const results = useMemo(() => {
    if (!q) return null
    const seen = new Set<string>()
    const scored: { e: Entry; score: number }[] = []
    for (const e of entries) {
      // A parent row whose first child is the same page is reached through the child.
      if (!e.parent && e.node.children?.length) continue
      const key = entryKey(e)
      if (seen.has(key)) continue
      const label = e.node.label.toLowerCase()
      const context = [e.group.label, e.group.short, e.parent?.label ?? '', e.parent?.hint ?? '', e.node.hint ?? '']
        .join(' ')
        .toLowerCase()
      let score = -1
      if (label.startsWith(q)) score = 0
      else if (label.includes(q)) score = 1
      else if (context.includes(q)) score = 2
      if (score < 0) continue
      seen.add(key)
      scored.push({ e, score })
    }
    // Stable within a score, so results keep the menu's order.
    return scored.sort((a, b) => a.score - b.score).map((s) => s.e).slice(0, 30)
  }, [q, entries])

  useEffect(() => { setCursor(0) }, [q])

  // Once a result is picked the nav returns to its normal tree.
  useEffect(() => { setQuery('') }, [pathname, search])

  const resultsRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    resultsRef.current?.querySelector(`[data-index="${cursor}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  // `/` focuses the menu search and `[` collapses or expands the sidebar, from anywhere outside a text field.
  useEffect(() => {
    if (variant === 'drawer') return
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)
      if (typing) return
      if (e.key === '/' && wide) {
        e.preventDefault()
        searchRef.current?.focus()
      } else if (e.key === '[') {
        e.preventDefault()
        onToggleExpanded()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [wide, variant, onToggleExpanded])

  /* --- keep the current page in view ------------------------------ */
  const navRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const t = window.setTimeout(() => {
      navRef.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest' })
    }, 220)
    return () => window.clearTimeout(t)
  }, [pathname, search, wide])

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

  const starredGroup: NavGroup | null = starred.length
    ? {
        id: 'starred',
        label: 'Starred',
        short: 'Starred',
        icon: Star,
        zone: 'operate',
        items: starred.map(({ node, group, parent }) => ({
          ...node,
          children: undefined,
          hint: parent ? `${group.label} › ${parent.label}` : group.label,
        })),
      }
    : null

  /* --- render ------------------------------------------------------ */
  const zones = NAV_ZONES.map((z) => ({ ...z, groups: groups.filter((g) => g.zone === z.id) })).filter((z) => z.groups.length)

  const searchResults = results && (
    <div ref={resultsRef} role="listbox" aria-label="Matching pages" className="space-y-px">
      <SectionLabel>{results.length ? `${results.length} matching page${results.length === 1 ? '' : 's'}` : 'No matches'}</SectionLabel>
      {results.map((e, i) => {
        const Icon = e.node.icon
        return (
          <Link
            key={entryKey(e)}
            to={e.node.to}
            data-index={i}
            role="option"
            aria-selected={i === cursor}
            onMouseMove={() => setCursor(i)}
            onClick={() => { setQuery(''); onNavigate?.() }}
            className={cn(
              'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 transition-colors',
              i === cursor ? 'bg-primary/10 text-primary' : 'text-sidebar-text/80 hover:bg-sidebar-hover',
            )}
          >
            <Icon className={cn('h-4 w-4 shrink-0', i === cursor ? 'text-primary' : 'text-sidebar-text-muted')} />
            <span className="min-w-0 flex-1 leading-tight">
              <span className="block truncate text-sm font-medium">{e.node.label}</span>
              <span className="block truncate text-xs text-sidebar-text-muted">{entryTrail(e)}</span>
            </span>
            {i === cursor && <CornerDownLeft className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden />}
          </Link>
        )
      })}
      {results.length === 0 && (
        <div className="px-3 py-6 text-center text-xs text-sidebar-text-muted">
          No pages match “{query}”.
        </div>
      )}
    </div>
  )

  const tree = (
    <>
      {starred.length > 0 && (
        <div>
          <SectionLabel>Starred</SectionLabel>
          <div className="space-y-px">
            {starred.map((e) => (
              <LeafRow
                key={`star:${e.node.to}`}
                node={e.node.children?.length ? { ...e.node, children: undefined } : e.node}
                pathname={pathname}
                params={params}
                alertCount={alertCount}
                onNavigate={onNavigate}
                showIcon
                caption={entryTrail(e)}
                starred
                onToggleStar={toggleStar}
              />
            ))}
          </div>
        </div>
      )}

      {zones.map((zone) => (
        <div key={zone.id}>
          <SectionLabel>{zone.label}</SectionLabel>
          <div className="space-y-px">
            {zone.groups.map((group) => {
              const open = openGroup === group.id
              const groupActive = activeGroup?.id === group.id
              return (
                <div key={group.id}>
                  <button
                    type="button"
                    onClick={() => setOpenGroup((cur) => (cur === group.id ? null : group.id))}
                    aria-expanded={open}
                    className={cn(
                      'group/group flex h-9 w-full items-center gap-2.5 rounded-lg px-1.5 text-left transition-colors hover:bg-sidebar-hover',
                      groupActive && !open && 'bg-sidebar-hover',
                    )}
                  >
                    <span
                      className={cn(
                        'flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors',
                        groupActive
                          ? 'bg-primary text-[rgb(var(--on-primary))] shadow-sm'
                          : 'bg-sidebar-hover text-sidebar-text/70 group-hover/group:text-sidebar-text',
                      )}
                    >
                      <group.icon className="h-4 w-4" />
                    </span>
                    <span
                      className={cn(
                        'min-w-0 flex-1 truncate text-sm',
                        groupActive ? 'font-semibold text-sidebar-text' : 'font-medium text-sidebar-text/85',
                      )}
                    >
                      {group.label}
                    </span>
                    {group.id === 'alerting' && !open && <AlertBadge count={alertCount} />}
                    <ChevronRight
                      className={cn(
                        'h-3.5 w-3.5 shrink-0 text-sidebar-text-muted transition-transform duration-200',
                        open && 'rotate-90',
                      )}
                    />
                  </button>

                  <div className="nav-collapse" data-open={open}>
                    <div>
                      <div className="mb-1.5 ml-[20px] mt-0.5 space-y-px border-l border-sidebar-border pl-2.5">
                        {group.items.map((item) => (
                          <Fragment key={item.to}>
                            {item.section && <SectionLabel className="pl-2.5 pt-2">{item.section}</SectionLabel>}
                            {item.children && item.children.length > 0 ? (
                              <BranchRow
                                node={item}
                                pathname={pathname}
                                params={params}
                                alertCount={alertCount}
                                onNavigate={onNavigate}
                                open={isItemOpen(item)}
                                onToggle={() =>
                                  setManualOpen((m) => ({ ...m, [item.to]: !isItemOpen(item) }))
                                }
                                stars={stars}
                                onToggleStar={toggleStar}
                              />
                            ) : (
                              <LeafRow
                                node={item}
                                pathname={pathname}
                                params={params}
                                alertCount={alertCount}
                                onNavigate={onNavigate}
                                starred={stars.includes(item.to)}
                                onToggleStar={toggleStar}
                              />
                            )}
                          </Fragment>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </>
  )

  const railButton = (group: NavGroup, active: boolean) => (
    <button
      key={group.id}
      type="button"
      aria-label={group.label}
      onMouseEnter={(e) => openFlyout(group, e.currentTarget)}
      onMouseLeave={scheduleClose}
      onFocus={(e) => openFlyout(group, e.currentTarget)}
      onClick={() => navigate(group.items[0].to)}
      className={cn(
        'group/rail relative flex w-full flex-col items-center gap-1 rounded-lg py-1.5 transition-colors',
        active ? 'text-primary' : 'text-sidebar-text/70 hover:text-sidebar-text',
      )}
    >
      <span
        className={cn(
          'relative flex h-9 w-9 items-center justify-center rounded-lg transition-colors',
          active
            ? 'bg-primary text-[rgb(var(--on-primary))] shadow-sm'
            : 'group-hover/rail:bg-sidebar-hover',
        )}
      >
        <group.icon className={cn('h-[18px] w-[18px]', group.id === 'starred' && !active && 'text-warning')} />
        {group.id === 'alerting' && alertCount > 0 && (
          <span className="absolute -right-1.5 -top-1 min-w-[16px] rounded-full bg-danger px-1 text-center text-2xs font-bold leading-4 text-white ring-2 ring-sidebar-bg">
            {alertCount > 99 ? '99+' : alertCount}
          </span>
        )}
      </span>
      <span className={cn('max-w-full truncate px-0.5 text-2xs leading-none', active ? 'font-semibold' : 'font-medium')}>
        {group.short}
      </span>
    </button>
  )

  const nav = wide ? (
    <nav ref={navRef} aria-label="Main menu" className="sidebar-scroll flex-1 overflow-y-auto overflow-x-hidden px-2.5 pb-3">
      {searchResults ?? tree}
    </nav>
  ) : (
    <nav ref={navRef} aria-label="Main menu" className="sidebar-scroll flex-1 overflow-y-auto overflow-x-hidden px-2 py-2">
      {starredGroup && (
        <>
          {railButton(starredGroup, false)}
          <div className="mx-3 my-1.5 border-t border-sidebar-border" />
        </>
      )}
      {zones.map((zone, zi) => (
        <Fragment key={zone.id}>
          {zi > 0 && <div className="mx-3 my-1.5 border-t border-sidebar-border" />}
          <div className="space-y-0.5">
            {zone.groups.map((group) => railButton(group, activeGroup?.id === group.id))}
          </div>
        </Fragment>
      ))}
    </nav>
  )

  const initials = (user?.full_name || user?.username || 'U')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join('')

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
            className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sidebar-text-muted hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <X className="h-4 w-4" />
          </button>
        ) : wide ? (
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-label="Collapse sidebar"
            title="Collapse sidebar  [ "
            className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <PanelLeftClose className="h-4 w-4" />
          </button>
        ) : null}
      </div>

      {/* Search */}
      {wide ? (
        <div className="shrink-0 px-2.5 pb-1 pt-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sidebar-text-muted" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') { setQuery(''); e.currentTarget.blur() }
                if (!results?.length) return
                if (e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(c + 1, results.length - 1)) }
                if (e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)) }
                if (e.key === 'Enter') {
                  const pick = results[Math.min(cursor, results.length - 1)]
                  if (pick) { navigate(pick.node.to); setQuery(''); e.currentTarget.blur(); onNavigate?.() }
                }
              }}
              placeholder="Search menu…"
              aria-label="Search the menu"
              role="combobox"
              aria-expanded={!!results}
              aria-autocomplete="list"
              className="h-9 w-full rounded-lg border border-sidebar-border bg-sidebar-hover/50 pl-8 pr-8 text-sm text-sidebar-text placeholder:text-sidebar-text-muted transition-colors focus:border-primary/50 focus:bg-sidebar-bg focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
            {query ? (
              <button
                type="button"
                onClick={() => setQuery('')}
                aria-label="Clear search"
                className="absolute right-1.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-sidebar-text-muted hover:text-sidebar-text"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : (
              <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-sidebar-border bg-sidebar-bg px-1.5 font-mono text-2xs font-semibold leading-4 text-sidebar-text-muted">
                /
              </kbd>
            )}
          </div>
        </div>
      ) : (
        <div className="shrink-0 px-2 pt-2">
          <button
            type="button"
            onClick={() => { onToggleExpanded(); window.setTimeout(() => searchRef.current?.focus(), 230) }}
            aria-label="Search the menu"
            title="Search the menu"
            className="flex h-9 w-full items-center justify-center rounded-lg text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
          >
            <Search className="h-4 w-4" />
          </button>
        </div>
      )}

      {nav}

      {/* Footer */}
      <div className={cn('shrink-0 border-t border-sidebar-border', wide ? 'p-2.5' : 'space-y-1 p-2')}>
        {wide ? (
          <div className="flex items-center gap-2.5 rounded-lg px-1.5 py-1">
            <div className="relative shrink-0">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-primary/25 to-accent/25 text-xs font-bold text-primary">
                {initials}
              </div>
              <span className="absolute -bottom-px -right-px h-2.5 w-2.5 rounded-full bg-success ring-2 ring-sidebar-bg" aria-hidden />
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-sm font-semibold text-sidebar-text">
                {user?.full_name || user?.username}
              </div>
              <div className="truncate text-xs text-sidebar-text-muted">
                <span className="capitalize">{(user?.role || 'user').replace(/_/g, ' ')}</span>
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
          <>
            <div
              className="mx-auto flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-primary/25 to-accent/25 text-xs font-bold text-primary"
              title={`${user?.full_name || user?.username || ''} (${user?.role || 'user'})`}
            >
              {initials}
            </div>
            <button
              type="button"
              onClick={onToggleExpanded}
              aria-label="Expand sidebar"
              title="Expand sidebar  [ "
              className="flex w-full items-center justify-center rounded-lg py-2 text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
            >
              <PanelLeftOpen className="h-4 w-4" />
            </button>
          </>
        )}
      </div>
    </>
  )

  if (variant === 'drawer') {
    return (
      <aside
        className="flex h-full w-[19rem] flex-col border-r border-sidebar-border bg-sidebar-bg"
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
