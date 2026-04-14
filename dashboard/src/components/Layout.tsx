import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  Bell,
  Boxes,
  FileText,
  LayoutDashboard,
  LogOut,
  Moon,
  Radar,
  Search,
  Server,
  Settings as SettingsIcon,
  Sun,
  Upload,
} from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { useTheme } from '@/stores/theme'
import { Button } from '@/components/ui/Button'
import { cn } from '@/lib/utils'

type Item = {
  to: string
  label: string
  icon: React.ComponentType<{ className?: string }>
  end?: boolean
}

const primaryNav: Item[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/devices', label: 'Devices', icon: Server },
  { to: '/services', label: 'Services', icon: Activity },
  { to: '/discovery', label: 'Discovery', icon: Radar },
]

const alertingNav: Item[] = [
  { to: '/alerts', label: 'Alerts', icon: AlertTriangle },
  { to: '/alert-rules', label: 'Rules', icon: Bell },
]

const systemNav: Item[] = [
  { to: '/mibs', label: 'MIB Library', icon: Upload },
  { to: '/reports', label: 'Reports', icon: FileText },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
]

function NavGroup({ label, items }: { label: string; items: Item[] }) {
  return (
    <div className="space-y-0.5">
      <div className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted2">
        {label}
      </div>
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors',
              isActive
                ? 'bg-primary/10 text-primary'
                : 'text-text2 hover:bg-surface2 hover:text-text',
            )
          }
        >
          <item.icon className="h-4 w-4 shrink-0" />
          <span>{item.label}</span>
        </NavLink>
      ))}
    </div>
  )
}

export function Layout() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()

  return (
    <div className="flex min-h-screen bg-bg">
      {/* Sidebar */}
      <aside className="hidden w-56 shrink-0 border-r border-border bg-surface md:flex md:flex-col">
        <div className="flex h-12 items-center gap-2 border-b border-border px-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary">
            <Boxes className="h-4 w-4" />
          </div>
          <span className="text-base font-semibold tracking-tight">ZenPlus</span>
        </div>
        <nav className="flex-1 space-y-4 overflow-y-auto p-3">
          <NavGroup label="Monitor" items={primaryNav} />
          <NavGroup label="Alerting" items={alertingNav} />
          <NavGroup label="System" items={systemNav} />
        </nav>
        <div className="border-t border-border p-3">
          <div className="flex items-center gap-2 rounded-md bg-surface2 px-2.5 py-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/20 text-[13px] font-semibold text-primary">
              {(user?.username || 'U').slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium">{user?.username || 'guest'}</div>
              <div className="truncate text-[11px] text-muted">{user?.role}</div>
            </div>
          </div>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-12 items-center gap-3 border-b border-border bg-surface/95 px-5 backdrop-blur">
          <div className="flex items-center gap-2 md:hidden">
            <Boxes className="h-5 w-5 text-primary" />
            <span className="font-semibold">ZenPlus</span>
          </div>
          <div className="relative ml-auto flex max-w-md flex-1 items-center">
            <Search className="absolute left-2.5 h-3.5 w-3.5 text-muted" />
            <input
              placeholder="Search devices, alerts, traps…"
              className="h-8 w-full rounded-md border border-border bg-surface2 pl-8 pr-3 text-sm placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-primary/40"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const q = (e.target as HTMLInputElement).value
                  if (q) navigate(`/devices?search=${encodeURIComponent(q)}`)
                }
              }}
            />
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={toggle}
            title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
          >
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => { logout(); navigate('/login') }}
            title="Logout"
          >
            <LogOut className="h-4 w-4" />
          </Button>
        </header>

        <main className="flex-1 overflow-y-auto p-5">
          <div className="mx-auto max-w-[1400px] animate-fade-in">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
