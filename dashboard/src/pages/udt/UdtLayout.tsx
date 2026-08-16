import { Activity, ListChecks, ScanSearch, Server, Settings, Shapes, UserRound } from 'lucide-react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { KbLink, type KbArticle } from '@/components/udt/KbLink'

type TabDef = {
  to: string
  label: string
  icon: React.ComponentType<{ className?: string }>
  end?: boolean
  /** Knowledge-base article that documents this tab. */
  article: KbArticle
}

const tabs: TabDef[] = [
  { to: '/udt', label: 'Endpoints', icon: ScanSearch, end: true, article: 'endpoints' },
  { to: '/udt/ports', label: 'Switch Ports', icon: Server, article: 'switch-ports' },
  { to: '/udt/users', label: 'User Logins', icon: UserRound, article: 'user-logins' },
  { to: '/udt/classification', label: 'Classification', icon: Shapes, article: 'classification' },
  { to: '/udt/watch-lists', label: 'Watch Lists', icon: ListChecks, article: 'watch-lists' },
  { to: '/udt/activity', label: 'Activity', icon: Activity, article: 'activity' },
  { to: '/udt/settings', label: 'Settings', icon: Settings, article: 'getting-started' },
]

/** The article documenting the screen currently on show. */
function articleForPath(pathname: string): KbArticle {
  if (/^\/udt\/endpoints\/[^/]+/.test(pathname)) return 'endpoints'
  // Longest match first so /udt does not shadow the sub-tabs.
  const match = [...tabs]
    .sort((a, b) => b.to.length - a.to.length)
    .find((t) => (t.end ? pathname === t.to : pathname.startsWith(t.to)))
  return match?.article ?? 'overview'
}

export function UdtLayout() {
  const location = useLocation()
  // Endpoint detail owns the full canvas.
  const hideStrip = /\/udt\/endpoints\/[^/]+/.test(location.pathname)
  const article = articleForPath(location.pathname)

  return (
    <div className="space-y-4">
      {!hideStrip && (
        <>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
                <ScanSearch className="h-6 w-6 text-primary" />
                User Device Tracker
                <KbLink article={article} label="User Device Tracker documentation" />
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
