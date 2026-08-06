import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Activity, AlertTriangle, Boxes, GitBranch, LayoutDashboard, Network, Settings, Target, Users } from 'lucide-react'
import { cn } from '@/lib/utils'

type TabDef = { to: string; label: string; icon: React.ComponentType<{ className?: string }>; end?: boolean }

const tabs: TabDef[] = [
  { to: '/apm', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/apm/services', label: 'Services', icon: Boxes },
  { to: '/apm/service-map', label: 'Service Map', icon: Network },
  { to: '/apm/traces', label: 'Traces', icon: GitBranch },
  { to: '/apm/errors', label: 'Errors', icon: AlertTriangle },
  { to: '/apm/slos', label: 'SLOs', icon: Target },
  { to: '/apm/synthetics', label: 'Synthetics', icon: Activity },
  { to: '/apm/usage', label: 'Usage', icon: Users },
  { to: '/apm/settings', label: 'Settings', icon: Settings },
]

/**
 * Module chrome for APM: one title, one tab strip, everywhere.
 *
 * Detail views (a single trace, a single error, a single service) take the full
 * canvas — they have their own back-navigation and the strip would just compete
 * with it.
 */
export function ApmLayout() {
  const { pathname } = useLocation()
  const isDetail =
    /^\/apm\/traces\/[^/]+/.test(pathname) ||
    /^\/apm\/errors\/[^/]+/.test(pathname) ||
    /^\/apm\/services\/[^/]+/.test(pathname)

  return (
    <div className="space-y-4">
      {!isDetail && (
        <>
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Activity className="h-6 w-6 text-primary" />
              Application Monitoring
            </h1>
            <p className="mt-1 text-xs text-muted">
              Distributed tracing, golden signals, error tracking and reliability targets for every instrumented service.
            </p>
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
