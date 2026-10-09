import type { ReactNode } from 'react'
import { Link, Outlet } from 'react-router-dom'
import { Lock, PackageSearch } from 'lucide-react'
import { gateAllows, type NavGate } from '@/components/layout/navAccess'
import { hasPermission, useAuth, type User } from '@/stores/auth'

// ─────────────────────────────────────────────────────────────────
// Access state for the Inventory module (doc 03 section 1.2). Follows the NetFlow gate: one
// designed denied state without the view permission, a layout route that wraps every /inventory
// page, and the gate expressed with navAccess.ts so the sidebar, the direct URL and this state
// agree on what "may open Inventory" means. No page renders and no request is sent when denied.
// ─────────────────────────────────────────────────────────────────

export const INVENTORY_VIEW_PERMISSION = 'inventory.view'
/** The same gate shape the navigation entries use. */
export const INVENTORY_VIEW_GATE: NavGate = { permission: INVENTORY_VIEW_PERMISSION }
export const ACCESS_DENIED_TITLE = "You don't have access to Inventory"
export const ACCESS_DENIED_DETAIL =
  'Inventory shows every operating system, software title and hardware model across servers and network devices. Your role does not include the Inventory view permission, so none of it is shown.'

export type InventoryAccess = 'unknown' | 'allowed' | 'denied'

/**
 * Whether the account may open Inventory. Until /auth/me has answered the permissions are not known;
 * that is "unknown", not "denied" (RouteGate renders the page in the same situation and the server
 * refuses the requests on its own), so a slow start never flashes a denial.
 */
export function inventoryAccess(user: Pick<User, 'permissions'> | null | undefined): InventoryAccess {
  if (!user?.permissions) return 'unknown'
  return gateAllows(INVENTORY_VIEW_GATE, (p) => hasPermission(user as User, p)) ? 'allowed' : 'denied'
}

/**
 * The account's Inventory access as a hook. A page that fetches data should wait for `allowed`
 * (`enabled: access === 'allowed'`): while it is `unknown` (the permissions have not loaded) a request
 * would only be refused, and when it is `denied` no request should be sent at all.
 */
export function useInventoryAccess(): InventoryAccess {
  return inventoryAccess(useAuth((s) => s.user))
}

function StatePanel({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="rounded-lg border border-border bg-surface p-5 shadow-card sm:p-6">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-warning/10 text-warning-fg">
          <Lock className="h-5 w-5" aria-hidden />
        </span>
        <div className="min-w-0 space-y-2">{children}</div>
      </div>
    </div>
  )
}

/** The single state for an account without inventory.view on any /inventory route. */
export function InventoryAccessDenied() {
  return (
    <div className="space-y-4">
      <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
        <PackageSearch className="h-6 w-6 text-primary" aria-hidden />
        Inventory
      </h1>
      <StatePanel>
        <h2 className="text-sm font-semibold text-text">{ACCESS_DENIED_TITLE}</h2>
        <div className="max-w-2xl space-y-2 text-xs text-text2">
          <p>{ACCESS_DENIED_DETAIL}</p>
          <p>
            Ask an administrator to add{' '}
            <code className="rounded bg-surface2 px-1 py-0.5 font-mono text-[11px] font-semibold text-text">{INVENTORY_VIEW_PERMISSION}</code>{' '}
            to your role.
          </p>
          <p>
            <Link to="/" className="font-medium text-primary hover:underline">
              Back to the dashboard
            </Link>
          </p>
        </div>
      </StatePanel>
    </div>
  )
}

/**
 * Layout route for every /inventory page: the denied state without inventory.view (no page renders,
 * no request is sent), the page otherwise. While the account's permissions are still loading the
 * page renders, as RouteGate does; the server stays the authorization boundary.
 */
export function InventoryAccessGate() {
  const user = useAuth((s) => s.user)
  if (inventoryAccess(user) === 'denied') return <InventoryAccessDenied />
  return <Outlet />
}
