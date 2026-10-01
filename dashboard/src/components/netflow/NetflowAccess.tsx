import type { ReactNode } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Lock, Network, SearchX, ShieldOff } from 'lucide-react'
import { api } from '@/lib/api'
import { useCan } from '@/stores/auth'
import { httpStatus } from '@/lib/netflow/status'
import { NetflowPageHeader } from './NetflowPageHeader'

// ─────────────────────────────────────────────────────────────────
// Access states for the NetFlow module (X-01, NF-04): one clear state
// for each reason a page has nothing to show that is NOT a collection
// problem — no permission, a scope without flow exporters, an exporter
// outside the scope. Without them the pages rendered dozens of 403/404
// panels next to "No exporter has sent flows to this appliance" and
// "collection gaps", which described an access limit as a data loss.
// ─────────────────────────────────────────────────────────────────

/** `GET /netflow/scope`: what the caller's NetFlow scope holds. */
export interface NetflowScope {
  restricted: boolean
  /** Exporters with flows in the caller's scope; null when unrestricted or unknown. */
  flow_exporters: number | null
  /** True when the scope holds no exporter that has sent flows; null when unknown. */
  scope_empty: boolean | null
  can_manage: boolean
  can_export: boolean
}

export const NETFLOW_VIEW_PERMISSION = 'netflow.view'
export const SCOPE_EMPTY_TITLE = 'Your scope includes no flow exporters'
export const SCOPE_EMPTY_DETAIL =
  'None of the devices your tag scope covers owns an exporter that has sent flows to this appliance, so NetFlow pages have nothing to show you. This is an access scope, not a collection problem: an administrator can widen your scope or map an exporter to one of your devices.'
export const ACCESS_DENIED_TITLE = 'NetFlow access required'
export const ACCESS_DENIED_DETAIL =
  'Your role does not include the NetFlow view permission (netflow.view), so flow data is not shown. An administrator can grant it.'

/** Whether the overview should show the empty-scope state instead of its panels. */
export function isScopeEmpty(scope: NetflowScope | null | undefined): boolean {
  return !!scope && scope.restricted && scope.scope_empty === true
}

/** Whether a failed NetFlow request means "not found / outside your scope" (404). */
export function isNotFoundError(err: unknown): boolean {
  return httpStatus(err) === 404
}

export function useNetflowScope(enabled = true) {
  return useQuery({
    queryKey: ['netflow', 'scope'],
    queryFn: async () => (await api.get('/netflow/scope')).data as NetflowScope,
    staleTime: 60_000,
    retry: false,
    enabled,
  })
}

function StatePanel({ icon: Icon, title, children, tone = 'neutral' }: { icon: typeof Lock; title: string; children: ReactNode; tone?: 'neutral' | 'warning' }) {
  return (
    <div
      role="status"
      className={
        tone === 'warning'
          ? 'rounded-lg border border-warning/35 bg-warning/10 p-5 sm:p-6'
          : 'rounded-lg border border-border bg-surface p-5 shadow-card sm:p-6'
      }
    >
      <div className="flex items-start gap-3">
        <Icon className={tone === 'warning' ? 'mt-0.5 h-5 w-5 shrink-0 text-warning-fg' : 'mt-0.5 h-5 w-5 shrink-0 text-text2'} aria-hidden />
        <div className="min-w-0 space-y-1.5">
          <h2 className="text-sm font-semibold text-text">{title}</h2>
          <div className="max-w-2xl space-y-2 text-xs text-text2">{children}</div>
        </div>
      </div>
    </div>
  )
}

/** The single state for a user without netflow.view on any /netflow route. */
export function NetflowAccessDenied() {
  return (
    <div className="space-y-4">
      <NetflowPageHeader title="NetFlow" icon={Network} crumb="Access" />
      <StatePanel icon={Lock} title={ACCESS_DENIED_TITLE}>
        <p>{ACCESS_DENIED_DETAIL}</p>
        <p>
          <Link to="/" className="font-medium text-primary hover:underline">
            Back to the dashboard
          </Link>
        </p>
      </StatePanel>
    </div>
  )
}

/** Full-page state for a restricted user whose scope holds no flow exporter. */
export function ScopeEmptyState({ title = 'NetFlow overview' }: { title?: ReactNode }) {
  return (
    <div className="space-y-4">
      <NetflowPageHeader title={title} icon={Network} crumb="Overview" />
      <StatePanel icon={ShieldOff} title={SCOPE_EMPTY_TITLE}>
        <p>{SCOPE_EMPTY_DETAIL}</p>
      </StatePanel>
    </div>
  )
}

/** Banner above other NetFlow pages for the same situation. */
export function ScopeEmptyBanner() {
  return (
    <div role="status" className="mb-4 flex items-start gap-2 rounded-md border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-text">
      <ShieldOff className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
      <span>
        <span className="font-medium">{SCOPE_EMPTY_TITLE}.</span> Empty panels below reflect your access scope, not missing data.
      </span>
    </div>
  )
}

/** The single state for an exporter deep-dive the caller cannot see (or that does not exist). */
export function ExporterNotFoundState({ ip }: { ip: string }) {
  return (
    <div className="space-y-4">
      <NetflowPageHeader title="Exporter not found" icon={SearchX} crumb={`Exporter ${ip}`} />
      <StatePanel icon={SearchX} title={`No exporter ${ip} in your view`}>
        <p>
          This address is not a flow exporter you can see: it may never have sent flows here, or it belongs to a device outside your
          scope. Nothing about its traffic is shown.
        </p>
        <p>
          <Link to="/netflow" className="font-medium text-primary hover:underline">
            Back to the NetFlow overview
          </Link>
        </p>
      </StatePanel>
    </div>
  )
}

/**
 * Layout route for every /netflow page: one access-denied state without netflow.view (no page
 * renders, no request is sent), and a banner on every page but the overview — which shows the
 * full empty-scope state itself — when the scope holds no flow exporter.
 */
export function NetflowAccessGate() {
  const can = useCan()
  const allowed = can(NETFLOW_VIEW_PERMISSION)
  const scope = useNetflowScope(allowed)
  const { pathname } = useLocation()
  if (!allowed) return <NetflowAccessDenied />
  const overview = pathname.replace(/\/+$/, '') === '/netflow'
  return (
    <>
      {isScopeEmpty(scope.data) && !overview && <ScopeEmptyBanner />}
      <Outlet />
    </>
  )
}
