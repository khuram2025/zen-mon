import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Activity, AlertTriangle, Boxes, GitBranch, Globe2, LayoutDashboard, Network, Settings, Target, Users } from 'lucide-react'
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
  { to: '/apm/rum', label: 'RUM', icon: Globe2 },
  { to: '/apm/usage', label: 'Usage', icon: Users },
  { to: '/apm/settings', label: 'Settings', icon: Settings },
]

/**
 * Module chrome for APM: compact title + prominent tab strip on list views.
 * Detail views keep a slimmer context strip so back-navigation stays first.
 */
export function ApmLayout() {
  const { pathname } = useLocation()
  const isDetail =
    /^\/apm\/traces\/[^/]+/.test(pathname) ||
    /^\/apm\/errors\/[^/]+/.test(pathname) ||
    /^\/apm\/services\/[^/]+/.test(pathname) ||
    /^\/apm\/slos\/[^/]+/.test(pathname)

  return (
    <div className="space-y-4">
      {!isDetail && (
        <div className="space-y-3">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-muted">Applications</p>
            <h1 className="mt-0.5 text-2xl font-semibold tracking-tight">Application Performance</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted">
              Golden signals, traces, errors and reliability — from fleet health down to a single span.
            </p>
          </div>
          <nav className="flex items-center gap-1 overflow-x-auto rounded-lg border border-border bg-surface2/50 p-1">
            {tabs.map((t) => (
              <NavLink
                key={t.to}
                to={t.to}
                end={t.end}
                className={({ isActive }) =>
                  cn(
                    'inline-flex items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors',
                    isActive
                      ? 'bg-surface text-text shadow-sm ring-1 ring-border'
                      : 'text-muted hover:bg-surface/60 hover:text-text',
                  )
                }
              >
                <t.icon className="h-3.5 w-3.5" />
                {t.label}
              </NavLink>
            ))}
          </nav>
        </div>
      )}
      <Outlet />
    </div>
  )
}
