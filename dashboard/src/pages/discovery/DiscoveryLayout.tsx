import { Calendar, Clock, FileBarChart, Inbox, Key, Layers, Radar, SlashSquare } from 'lucide-react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

type TabDef = {
  to: string
  label: string
  icon: React.ComponentType<{ className?: string }>
  end?: boolean
}

const tabs: TabDef[] = [
  { to: '/discovery', label: 'Profiles', icon: Layers, end: true },
  { to: '/discovery/scheduled', label: 'Scheduled', icon: Calendar },
  { to: '/discovery/reports', label: 'Reports', icon: FileBarChart },
  { to: '/discovery/imports', label: 'Import Queue', icon: Inbox },
  { to: '/discovery/ignored', label: 'Ignored', icon: SlashSquare },
  { to: '/discovery/credentials', label: 'Credentials', icon: Key },
]

export function DiscoveryLayout() {
  const location = useLocation()
  // Hide the tab strip on run-detail / wizard sub-pages so they own
  // the full canvas.
  const hideStrip =
    /\/discovery\/(new|profiles\/[^/]+\/edit|runs\/[^/]+)/.test(location.pathname)

  return (
    <div className="space-y-4">
      {!hideStrip && (
        <>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
                <Radar className="h-6 w-6 text-primary" />
                Discovery
              </h1>
              <p className="mt-1 text-xs text-muted">
                Scan your network, classify devices, and import them into monitoring.
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
                    isActive
                      ? 'border-primary text-text'
                      : 'border-transparent text-muted hover:text-text',
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
