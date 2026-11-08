import { useMemo, type ReactNode } from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'
import { ShieldOff } from 'lucide-react'
import { allowedPages, hasPermission, useAuth, type User } from '@/stores/auth'
import { NAV_GROUPS, trailForLocation } from './navigation'
import { deniedPageName } from './deniedWording'
import { firstAllowedPath, gatePermissions, trailAllows } from './navAccess'

/**
 * Keeps direct URLs in step with the sidebar: a page whose navigation entry
 * the role may not see renders an explanation instead of a screen full of
 * failed requests, and the home page sends a role without the overview
 * dashboard to the first page it can open. The server still refuses the
 * underlying API calls on its own.
 */
export function RouteGate({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation()
  const user = useAuth((s) => s.user)
  const params = useMemo(() => new URLSearchParams(search), [search])

  // Until /auth/me has answered we do not know the role's permissions; let the
  // page render (it is guarded server-side) rather than flash a denial.
  if (!user?.permissions) return <>{children}</>

  const can = (p: string) => hasPermission(user, p)
  const pages = allowedPages(user)
  const trail = trailForLocation(pathname, params)
  if (trailAllows(trail, can, pages)) return <>{children}</>

  // The home page sends a role without the overview dashboard (or limited to selected pages) to its first page.
  const landing = firstAllowedPath(NAV_GROUPS, can, pages)
  if (pathname === '/' && landing && landing !== '/') return <Navigate to={landing} replace />

  // Limited to selected pages: the role holds the permission, the page is simply not on its list.
  if (pages && trail && pathname !== '/' && trailAllows(trail, can, null)) return <NoAccess page={deniedPageName(trail)} needed={[]} landing={landing} listed />

  const leaf = trail ? (trail.child ?? trail.item) : null
  const needed = leaf ? [...new Set([...gatePermissions(trail!.item), ...(trail!.child ? gatePermissions(trail!.child) : [])])] : []
  return <NoAccess page={deniedPageName(trail)} needed={needed} landing={landing} />
}

/** Whether the signed-in account may open `to` (a navigation address, query allowed): the same rule as the route
 *  gate, for links and header widgets that lead to a page. */
export function canOpenPath(user: User | null, to: string): boolean {
  if (!user?.permissions) return false
  const u = new URL(to, 'http://local')
  return trailAllows(trailForLocation(u.pathname, u.searchParams), (p) => hasPermission(user, p), allowedPages(user))
}

/** Hook form of {@link canOpenPath}. */
export function useCanOpen(): (to: string) => boolean {
  const user = useAuth((s) => s.user)
  return (to: string) => canOpenPath(user, to)
}

function NoAccess({ page, needed, landing, listed }: { page?: string; needed: string[]; landing: string | null; listed?: boolean }) {
  return (
    <div className="mx-auto mt-10 max-w-xl rounded-lg border border-border bg-surface p-6 shadow-card" role="status">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-warning/10 text-warning">
          <ShieldOff className="h-5 w-5" aria-hidden />
        </span>
        <div className="min-w-0 space-y-2">
          <h1 className="text-base font-semibold text-text">
            {landing ? `Your role does not include ${page ? `“${page}”` : 'this page'}` : 'No pages are assigned to your role'}
          </h1>
          <p className="text-sm text-text2">
            {listed && landing
              ? 'Your role is limited to selected pages, and this one is not among them, so it is hidden from your menu.'
              : landing
              ? 'This area is outside the access your administrator assigned, so it is hidden from your menu.'
              : 'Your account can sign in, but its role does not grant access to any part of the application yet.'}
            {' '}Ask an administrator to change your role if you need it.
          </p>
          {needed.length > 0 && (
            <p className="text-xs text-muted">
              Requires: {needed.map((p) => <code key={p} className="mr-1 rounded bg-surface2 px-1 py-0.5 font-mono text-xs">{p}</code>)}
            </p>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            {landing && (
              <Link to={landing} className="inline-flex h-8 items-center rounded-md bg-primary px-3 text-xs font-medium text-white hover:bg-primary/90">
                Go to my start page
              </Link>
            )}
            <Link to="/settings/general?tab=profile" className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-text hover:bg-surface2">
              My profile
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}
