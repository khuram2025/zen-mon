import { useState, useEffect, useMemo, useRef } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import {
  AlertCircle,
  AlertTriangle,
  Bell,
  ChevronRight,
  CheckCircle2,
  Info,
  LogOut,
  Menu,
  Moon,
  Search,
  Sun,
  User,
} from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { useTheme } from '@/stores/theme'
import { Button } from '@/components/ui/Button'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import { UpdateNotificationBell } from '@/components/UpdateNotificationBell'
import { Sidebar, SIDEBAR_RAIL, SIDEBAR_WIDE } from '@/components/layout/Sidebar'
import { trailForLocation } from '@/components/layout/navigation'

type HeaderAlert = {
  id: string
  severity: 'critical' | 'warning' | 'info'
  status: string
  message: string
  triggered_at: string
  device_hostname?: string | null
  device_ip?: string | null
  service_check_name?: string | null
}

/* ------------------------------------------------------------------ */
/*  Breadcrumbs — derived from the same tree the sidebar renders       */
/* ------------------------------------------------------------------ */

function Breadcrumbs() {
  const { pathname, search } = useLocation()
  const params = useMemo(() => new URLSearchParams(search), [search])
  const trail = trailForLocation(pathname, params)

  if (!trail) {
    return <span className="text-xs font-medium text-text">{pathname.split('/').pop() || 'ZenPlus'}</span>
  }

  const leaf = trail.child ?? trail.item
  // A detail route sits below the deepest nav row that owns it.
  const isDetail = pathname !== leaf.to.split('?')[0] && !leaf.to.includes('?')

  const crumbs: { label: string; muted: boolean }[] = [
    { label: trail.group.label, muted: true },
    ...(trail.child ? [{ label: trail.item.label, muted: true }] : []),
    { label: leaf.label, muted: isDetail },
    ...(isDetail ? [{ label: 'Detail', muted: false }] : []),
  ]

  return (
    <div className="flex items-center gap-1.5 text-xs">
      {crumbs.map((c, i) => (
        <span key={`${c.label}-${i}`} className="flex items-center gap-1.5">
          {i > 0 && <ChevronRight className="h-3 w-3 text-muted/40" />}
          <span className={c.muted ? 'text-muted' : 'font-medium text-text'}>{c.label}</span>
        </span>
      ))}
    </div>
  )
}

function HeaderAlertCenter() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  const { data: stats } = useQuery<any>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })

  const { data: alerts = [] } = useQuery<HeaderAlert[]>({
    queryKey: ['alerts', 'header-active'],
    queryFn: async () => {
      const r = (await api.get('/alerts?status=active&limit=6')).data
      return r?.data || []
    },
    refetchInterval: 15_000,
  })

  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  const active = Number(stats?.active || 0)
  const critical = Number(stats?.critical || 0)
  const warning = Number(stats?.warning || 0)
  const BellIcon = critical > 0 ? AlertCircle : active > 0 ? AlertTriangle : Bell

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`relative flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
          critical > 0 ? 'bg-danger/10 text-danger hover:bg-danger/15'
          : active > 0 ? 'bg-warning/10 text-warning hover:bg-warning/15'
          : 'text-muted hover:bg-surface2 hover:text-text'
        }`}
        title={`${active} active alerts`}
      >
        <BellIcon className="h-3.5 w-3.5" />
        {active > 0 && (
          <span className="absolute -right-1 -top-1 min-w-[16px] rounded-full bg-danger px-1 text-center text-[9px] font-bold leading-4 text-white">
            {active > 99 ? '99+' : active}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 w-[380px] overflow-hidden rounded-lg border border-border bg-surface shadow-xl animate-fade-in">
          <div className="border-b border-border p-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold">Alert Center</div>
                <div className="text-[11px] text-muted">{active} active · {critical} critical · {warning} warning</div>
              </div>
              <button
                type="button"
                onClick={() => { setOpen(false); navigate('/alerts') }}
                className="text-xs font-medium text-primary hover:underline"
              >
                Open all
              </button>
            </div>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {alerts.length === 0 ? (
              <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
                <CheckCircle2 className="h-7 w-7 text-success" />
                <div className="text-sm font-medium">No active alerts</div>
                <div className="text-xs text-muted">New device, service, and system alerts will appear here.</div>
              </div>
            ) : (
              alerts.map((alert) => {
                const Icon = alert.severity === 'critical' ? AlertCircle : alert.severity === 'warning' ? AlertTriangle : Info
                const tone = alert.severity === 'critical' ? 'text-danger bg-danger/10' : alert.severity === 'warning' ? 'text-warning bg-warning/10' : 'text-info bg-info/10'
                return (
                  <button
                    key={alert.id}
                    type="button"
                    onClick={() => { setOpen(false); navigate(`/alerts/${alert.id}`) }}
                    className="flex w-full items-start gap-3 border-b border-border/60 p-3 text-left transition-colors last:border-b-0 hover:bg-surface2/60"
                  >
                    <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${tone}`}>
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{alert.message}</span>
                      <span className="mt-0.5 block truncate text-[11px] text-muted">
                        {alert.device_hostname || alert.device_ip || alert.service_check_name || 'System'} · {relativeTime(alert.triggered_at)}
                      </span>
                    </span>
                  </button>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Layout                                                             */
/* ------------------------------------------------------------------ */

export function Layout() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()
  const { pathname, search } = useLocation()

  const [expanded, setExpanded] = useState(() => {
    try { return localStorage.getItem('zp-sidebar-pinned') !== 'false' } catch { return true }
  })
  useEffect(() => {
    localStorage.setItem('zp-sidebar-pinned', String(expanded))
  }, [expanded])

  const [drawer, setDrawer] = useState(false)
  useEffect(() => { setDrawer(false) }, [pathname])

  const [userMenu, setUserMenu] = useState(false)

  return (
    <div className="flex min-h-screen bg-bg">
      <Sidebar expanded={expanded} onToggleExpanded={() => setExpanded((v) => !v)} />

      {/* Mobile drawer */}
      {drawer && (
        <div className="fixed inset-0 z-50 flex md:hidden">
          <div className="absolute inset-0 bg-black/60 animate-fade-in" onClick={() => setDrawer(false)} />
          <div className="relative animate-fade-in">
            <Sidebar
              expanded
              variant="drawer"
              onToggleExpanded={() => setExpanded((v) => !v)}
              onNavigate={() => setDrawer(false)}
            />
          </div>
        </div>
      )}

      <div
        className="content-transition flex min-w-0 flex-1 flex-col md:ml-[var(--sidebar-w)]"
        style={{ ['--sidebar-w' as any]: `${expanded ? SIDEBAR_WIDE : SIDEBAR_RAIL}px` }}
      >
        {/* Header */}
        <header className="sticky top-0 z-20 flex h-11 items-center gap-3 border-b border-border bg-surface/95 px-4 backdrop-blur">
          <button
            type="button"
            onClick={() => setDrawer(true)}
            aria-label="Open navigation"
            className="flex h-7 w-7 items-center justify-center rounded-md text-muted hover:bg-surface2 hover:text-text md:hidden"
          >
            <Menu className="h-4 w-4" />
          </button>

          <div className="hidden md:block">
            <Breadcrumbs />
          </div>

          <div className="relative ml-auto flex max-w-xs flex-1 items-center">
            <Search className="absolute left-2.5 h-3.5 w-3.5 text-muted" />
            <input
              placeholder="Search devices..."
              className="h-7 w-full rounded-md border border-border bg-bg pl-8 pr-3 text-xs placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-primary/40"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const q = (e.target as HTMLInputElement).value
                  if (q) navigate(`/devices?search=${encodeURIComponent(q)}`)
                }
              }}
            />
          </div>

          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-text" onClick={toggle}>
            {theme === 'dark' ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
          </Button>

          <HeaderAlertCenter />

          {/* Update notification bell — deep-links to Settings → Updates */}
          <UpdateNotificationBell />

          {/* User */}
          <div className="relative">
            <button
              onClick={() => setUserMenu((o) => !o)}
              className="flex items-center gap-2 rounded-md px-1.5 py-1 transition-colors hover:bg-surface2"
            >
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
                {(user?.username || 'U')[0].toUpperCase()}
              </div>
              <span className="hidden text-sm font-medium md:inline">{user?.username}</span>
            </button>
            {userMenu && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setUserMenu(false)} />
                <div className="absolute right-0 top-full z-50 mt-1 w-52 overflow-hidden rounded-lg border border-border bg-surface shadow-xl animate-fade-in">
                  <div className="border-b border-border px-3 py-2">
                    <div className="text-sm font-medium">{user?.full_name || user?.username}</div>
                    <div className="text-xs text-muted">{user?.email}</div>
                    <span className="mt-1 inline-block rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-primary">
                      {user?.role}
                    </span>
                  </div>
                  <div className="py-1">
                    <button
                      className="flex w-full items-center gap-2.5 px-3 py-1.5 text-sm text-text2 hover:bg-surface2"
                      onClick={() => { setUserMenu(false); navigate('/settings/general?tab=profile') }}
                    >
                      <User className="h-4 w-4" /> Profile & Settings
                    </button>
                    <button
                      className="flex w-full items-center gap-2.5 px-3 py-1.5 text-sm text-danger hover:bg-surface2"
                      onClick={() => { setUserMenu(false); logout(); navigate('/login') }}
                    >
                      <LogOut className="h-4 w-4" /> Sign out
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </header>

        {/* Content — full width */}
        <main className="flex-1 overflow-y-auto p-5 animate-fade-in">
          <ErrorBoundary resetKey={`${pathname}${search}`}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
