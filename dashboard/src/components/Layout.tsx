import { useState, useEffect, useRef } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Bell,
  BellRing,
  ChevronRight,
  CheckCircle2,
  CreditCard,
  FileText,
  GitBranch,
  Info,
  LayoutDashboard,
  LogOut,
  Key,
  Mail,
  MapPinned,
  Moon,
  Network,
  Pin,
  PinOff,
  Radar,
  Search,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Sun,
  Upload,
  User,
  Users,
} from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { useTheme } from '@/stores/theme'
import { Button } from '@/components/ui/Button'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { UpdateNotificationBell } from '@/components/UpdateNotificationBell'

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
/*  Navigation structure                                               */
/* ------------------------------------------------------------------ */

type NavItem = {
  to: string
  label: string
  icon: React.ComponentType<{ className?: string }>
  end?: boolean
}

type NavSection = { label: string; items: NavItem[] }

const sections: NavSection[] = [
  {
    label: 'Overview',
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
    ],
  },
  {
    label: 'Monitoring',
    items: [
      { to: '/devices', label: 'Devices', icon: Server },
      { to: '/services', label: 'Services', icon: Activity },
      { to: '/netflow', label: 'NetFlow', icon: Network },
      { to: '/discovery', label: 'Discovery', icon: Radar },
    ],
  },
  {
    label: 'MAP',
    items: [
      { to: '/maps/automated', label: 'Automated Maps', icon: GitBranch },
      { to: '/maps/manual', label: 'Manual Maps', icon: MapPinned },
    ],
  },
  {
    label: 'Alerting',
    items: [
      { to: '/alerts', label: 'Alerts', icon: AlertTriangle },
      { to: '/alert-rules', label: 'Alert Rules', icon: Bell },
    ],
  },
  {
    label: 'Configuration',
    items: [
      { to: '/channels', label: 'Channels', icon: BellRing },
      { to: '/gateways', label: 'Gateways', icon: Mail },
      { to: '/snmp-profiles', label: 'SNMP Credentials', icon: Key },
      { to: '/windows-credentials', label: 'Windows Credentials', icon: ShieldCheck },
      { to: '/mibs', label: 'MIB Library', icon: Upload },
    ],
  },
  {
    label: 'Administration',
    items: [
      { to: '/users', label: 'Users', icon: Users },
      { to: '/settings/general', label: 'Settings', icon: SettingsIcon },
      { to: '/reports', label: 'Reports', icon: FileText },
      { to: '/subscription', label: 'Subscription', icon: CreditCard },
    ],
  },
]

/* ------------------------------------------------------------------ */
/*  Breadcrumbs                                                        */
/* ------------------------------------------------------------------ */

const routeLabels: Record<string, string> = {
  '/': 'Monitoring Overview',
  '/devices': 'Devices',
  '/services': 'Services',
  '/maps': 'Maps',
  '/maps/automated': 'Automated Maps',
  '/maps/manual': 'Manual Maps',
  '/topology': 'Automated Maps',
  '/netflow': 'NetFlow',
  '/discovery': 'Discovery',
  '/alerts': 'Alerts',
  '/alert-rules': 'Alert Rules',
  '/channels': 'Channels',
  '/notifications': 'Channels',
  '/gateways': 'Gateways',
  '/snmp-profiles': 'SNMP Credentials',
  '/windows-credentials': 'Windows Credentials',
  '/mibs': 'MIB Library',
  '/users': 'Users',
  '/settings/general': 'General Settings',
  '/reports': 'Reports',
  '/reports/executive': 'Executive',
  '/reports/technical': 'Technical',
  '/reports/business': 'Business',
  '/reports/inventory': 'Inventory',
  '/subscription': 'Subscription',
}

const routeSections: Record<string, string> = {
  '/': 'Overview',
  '/devices': 'Monitoring',
  '/services': 'Monitoring',
  '/maps': 'MAP',
  '/maps/automated': 'MAP',
  '/maps/manual': 'MAP',
  '/topology': 'MAP',
  '/netflow': 'Monitoring',
  '/discovery': 'Monitoring',
  '/alerts': 'Alerting',
  '/alert-rules': 'Alerting',
  '/channels': 'Configuration',
  '/notifications': 'Configuration',
  '/gateways': 'Configuration',
  '/snmp-profiles': 'Configuration',
  '/windows-credentials': 'Configuration',
  '/mibs': 'Configuration',
  '/users': 'Administration',
  '/settings/general': 'Administration',
  '/reports': 'Administration',
  '/reports/executive': 'Reports',
  '/reports/technical': 'Reports',
  '/reports/business': 'Reports',
  '/reports/inventory': 'Reports',
  '/subscription': 'Administration',
}

function Breadcrumbs() {
  const { pathname } = useLocation()
  const basePath = pathname.replace(/\/[a-f0-9-]{8,}$/, '')
  const section = routeSections[basePath] || routeSections[pathname] || 'Overview'
  const page = routeLabels[basePath] || routeLabels[pathname] || pathname.split('/').pop() || ''
  const isDetail = basePath !== pathname && routeLabels[basePath]

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span className="text-muted">{section}</span>
      <ChevronRight className="h-3 w-3 text-muted/40" />
      <span className={isDetail ? 'text-muted' : 'font-medium text-text'}>{page}</span>
      {isDetail && (
        <>
          <ChevronRight className="h-3 w-3 text-muted/40" />
          <span className="font-medium text-text">Detail</span>
        </>
      )}
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
/*  Sidebar                                                            */
/* ------------------------------------------------------------------ */

const W_COLLAPSED = 56
const W_EXPANDED = 228

function Sidebar({
  expanded,
  pinned,
  onMouseEnter,
  onMouseLeave,
  onTogglePin,
}: {
  expanded: boolean
  pinned: boolean
  onMouseEnter: () => void
  onMouseLeave: () => void
  onTogglePin: () => void
}) {
  return (
    <aside
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="sidebar-transition fixed inset-y-0 left-0 z-30 hidden flex-col border-r border-sidebar-border bg-sidebar-bg md:flex"
      style={{ width: expanded ? W_EXPANDED : W_COLLAPSED }}
    >
      {/* Logo */}
      <div className="flex h-12 items-center gap-2.5 overflow-hidden border-b border-sidebar-border px-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/15">
          <LayoutDashboard className="h-[18px] w-[18px] text-primary" />
        </div>
        <span
          className="sidebar-label whitespace-nowrap text-[15px] font-bold tracking-tight text-sidebar-text"
          style={{ opacity: expanded ? 1 : 0 }}
        >
          ZenPlus
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto overflow-x-hidden py-2">
        {sections.map((sec) => (
          <div key={sec.label} className="mb-0.5">
            <div
              className="sidebar-label overflow-hidden whitespace-nowrap px-4 py-1"
              style={{ opacity: expanded ? 1 : 0, height: expanded ? 22 : 0 }}
            >
              <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-sidebar-text-muted">
                {sec.label}
              </span>
            </div>

            {sec.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                title={expanded ? undefined : item.label}
                className={({ isActive }) =>
                  cn(
                    'group relative mx-1.5 mb-px flex items-center rounded-md py-[7px] text-[13px] font-medium transition-colors',
                    expanded ? 'gap-3 px-2.5' : 'justify-center px-0',
                    isActive
                      ? 'bg-primary/12 text-primary'
                      : 'text-sidebar-text/70 hover:bg-sidebar-hover hover:text-sidebar-text',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <div className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-primary" />
                    )}
                    <item.icon className="h-[18px] w-[18px] shrink-0" />
                    <span
                      className="sidebar-label whitespace-nowrap"
                      style={{ opacity: expanded ? 1 : 0, width: expanded ? 'auto' : 0 }}
                    >
                      {item.label}
                    </span>
                  </>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      {/* Pin */}
      <div
        className="sidebar-label border-t border-sidebar-border p-2"
        style={{ opacity: expanded ? 1 : 0, height: expanded ? 'auto' : 0, overflow: 'hidden' }}
      >
        <button
          onClick={onTogglePin}
          className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-sidebar-text-muted transition-colors hover:bg-sidebar-hover hover:text-sidebar-text"
        >
          {pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />}
          <span className="text-xs">{pinned ? 'Unpin sidebar' : 'Pin sidebar'}</span>
        </button>
      </div>
    </aside>
  )
}

/* ------------------------------------------------------------------ */
/*  Layout                                                             */
/* ------------------------------------------------------------------ */

export function Layout() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()

  const [pinned, setPinned] = useState(() => {
    try { return localStorage.getItem('zp-sidebar-pinned') === 'true' } catch { return false }
  })
  const [hovered, setHovered] = useState(false)
  const expanded = pinned || hovered

  useEffect(() => {
    localStorage.setItem('zp-sidebar-pinned', String(pinned))
  }, [pinned])

  const [userMenu, setUserMenu] = useState(false)

  return (
    <div className="flex min-h-screen bg-bg">
      <Sidebar
        expanded={expanded}
        pinned={pinned}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onTogglePin={() => setPinned((p) => !p)}
      />

      <div
        className="content-transition flex min-w-0 flex-1 flex-col"
        style={{ marginLeft: expanded ? W_EXPANDED : W_COLLAPSED }}
      >
        {/* Header */}
        <header className="sticky top-0 z-20 flex h-11 items-center gap-3 border-b border-border bg-surface/95 px-4 backdrop-blur">
          <div className="flex items-center gap-2 md:hidden">
            <LayoutDashboard className="h-5 w-5 text-primary" />
            <span className="font-semibold">ZenPlus</span>
          </div>

          <div className="hidden md:block">
            <Breadcrumbs />
          </div>

          <div className="relative ml-auto flex max-w-xs flex-1 items-center">
            <Search className="absolute left-2.5 h-3.5 w-3.5 text-muted" />
            <input
              placeholder="Search..."
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
                      onClick={() => { setUserMenu(false); navigate('/settings/general') }}
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
          <Outlet />
        </main>
      </div>
    </div>
  )
}
