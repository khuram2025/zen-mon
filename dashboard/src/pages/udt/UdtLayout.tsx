import { Activity, ListChecks, ScanSearch, Server, Settings, Shapes, UserRound } from 'lucide-react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

type TabDef = { to: string; label: string; icon: React.ComponentType<{ className?: string }>; end?: boolean }

const tabs: TabDef[] = [
  { to: '/udt', label: 'Endpoints', icon: ScanSearch, end: true },
  { to: '/udt/ports', label: 'Switch Ports', icon: Server },
  { to: '/udt/users', label: 'User Logins', icon: UserRound },
  { to: '/udt/classification', label: 'Classification', icon: Shapes },
  { to: '/udt/watch-lists', label: 'Watch Lists', icon: ListChecks },
  { to: '/udt/activity', label: 'Activity', icon: Activity },
  { to: '/udt/settings', label: 'Settings', icon: Settings },
]

export function UdtLayout() {
  const location = useLocation()
  // Endpoint detail owns the full canvas.
  const hideStrip = /\/udt\/endpoints\/[^/]+/.test(location.pathname)

  return (
    <div className="space-y-4">
      {!hideStrip && (
        <>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
                <ScanSearch className="h-6 w-6 text-primary" />
                User Device Tracker
              </h1>
              <p className="mt-1 text-xs text-muted">
                Track every endpoint on the network — which MAC, IP, user, switch and port, live and historically.
              </p>
            </div>
          </div>
          <div className="-mb-px flex items-center gap-1 overflow-x-auto border-b border-border">
            {tabs.map((t) => (
              <NavLink
                key={t.to}
                to={t.to}
                end={t.end}
                className={({ isActive }) =>
                  cn(
                    'inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                    isActive ? 'border-primary text-text' : 'border-transparent text-muted hover:text-text',
                  )
                }
              >
                <t.icon className="h-4 w-4" />
                {t.label}
              </NavLink>
            ))}
          </div>
        </>
      )}
      <Outlet />
    </div>
  )
}
