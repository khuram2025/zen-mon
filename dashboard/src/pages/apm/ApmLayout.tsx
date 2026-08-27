import { Outlet, useLocation } from 'react-router-dom'
import { Activity, AlertTriangle, Boxes, GitBranch, Globe2, LayoutDashboard, Network, Settings, Target, Users } from 'lucide-react'
import { ApmUnderlineNav } from '@/components/apm/explorer'

const tabs = [
  { key: 'overview', to: '/apm', label: 'Overview', icon: LayoutDashboard, end: true },
  { key: 'services', to: '/apm/services', label: 'Services', icon: Boxes },
  { key: 'map', to: '/apm/service-map', label: 'Service Map', icon: Network },
  { key: 'traces', to: '/apm/traces', label: 'Traces', icon: GitBranch },
  { key: 'errors', to: '/apm/errors', label: 'Errors', icon: AlertTriangle },
  { key: 'slos', to: '/apm/slos', label: 'SLOs', icon: Target },
  { key: 'synthetics', to: '/apm/synthetics', label: 'Synthetics', icon: Activity },
  { key: 'rum', to: '/apm/rum', label: 'RUM', icon: Globe2 },
  { key: 'usage', to: '/apm/usage', label: 'Usage', icon: Users },
  { key: 'settings', to: '/apm/settings', label: 'Settings', icon: Settings },
]

/**
 * Module chrome for APM. List views use an NSX-ALB-style underline tab strip.
 * Detail views keep a slimmer context so back-navigation stays first.
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
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-muted">Applications</p>
              <h1 className="mt-0.5 text-2xl font-semibold tracking-tight">Application Performance</h1>
              <p className="mt-1 max-w-3xl text-sm text-muted">
                Golden signals, traces, errors and real-user journeys — from fleet health down to a single request.
              </p>
            </div>
          </div>
          <ApmUnderlineNav items={tabs} />
        </div>
      )}
      <Outlet />
    </div>
  )
}
