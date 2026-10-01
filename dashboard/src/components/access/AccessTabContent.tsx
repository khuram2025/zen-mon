import { useSearchParams } from 'react-router-dom'
import { KeyRound, Lock, ShieldCheck, Users } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useCanAny } from '@/stores/auth'
import { UsersSection } from './UsersSection'
import { RolesSection } from './RolesSection'
import { AuthProvidersSection } from './AuthProvidersSection'

/** Permissions that make each sub-tab useful. The parent tab is visible when
 *  ANY child is (see ACCESS_TAB_PERMISSIONS), so a role that can only manage
 *  roles reaches Roles, and a user manager without "view users" still reaches Users. */
export const USERS_TAB_PERMISSIONS = ['users.view', 'users.manage'] as const
export const ROLES_TAB_PERMISSIONS = ['roles.view', 'roles.manage'] as const
export const AUTH_TAB_PERMISSIONS = ['system.admin'] as const
export const ACCESS_TAB_PERMISSIONS = [...USERS_TAB_PERMISSIONS, ...ROLES_TAB_PERMISSIONS, ...AUTH_TAB_PERMISSIONS] as const

type Sub = 'users' | 'roles' | 'auth'

/** Settings → Users & Access: user accounts, roles & permissions, and
 * authentication providers/policy. The sub-tab is kept in the `sub` URL
 * param so links deep into a section survive refresh. Each sub-tab shows only
 * when the role grants what it needs; a direct link to one that is not allowed
 * explains why instead of silently showing something else. */
export function AccessTabContent() {
  const canAny = useCanAny()
  const [searchParams, setSearchParams] = useSearchParams()
  const allowed: Record<Sub, boolean> = {
    users: canAny(USERS_TAB_PERMISSIONS),
    roles: canAny(ROLES_TAB_PERMISSIONS),
    auth: canAny(AUTH_TAB_PERMISSIONS),
  }
  const order: Sub[] = ['users', 'roles', 'auth']
  const available = order.filter((s) => allowed[s])

  const raw = searchParams.get('sub')
  const requested: Sub = raw === 'roles' || raw === 'auth' ? raw : 'users'
  const denied = !allowed[requested]
  const active: Sub = denied ? (available[0] ?? 'users') : requested

  function setActive(v: string) {
    const next = new URLSearchParams(searchParams)
    if (v === 'users') next.delete('sub')
    else next.set('sub', v)
    // Filters belong to one sub-tab; do not carry them across.
    for (const k of ['q', 'role', 'status', 'source', 'scope', 'signin', 'mfa', 'sort', 'order', 'page']) next.delete(k)
    setSearchParams(next, { replace: true })
  }

  if (available.length === 0) {
    return (
      <div role="alert" className="flex items-start gap-3 rounded-md border border-border bg-surface2/40 p-6 text-sm text-muted">
        <Lock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <div>
          <div className="font-medium text-text">Users &amp; Access is not available to your role</div>
          Ask an administrator for user or role permissions if you need to manage accounts.
        </div>
      </div>
    )
  }

  return (
    <Tabs value={active} onValueChange={setActive}>
      {denied && (
        <div role="alert" className="mb-2 flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs">
          <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
          Your role does not include access to the “{requested === 'auth' ? 'Authentication' : requested === 'roles' ? 'Roles & Permissions' : 'Users'}” section; showing the first section you can use.
        </div>
      )}
      <TabsList className="mb-1 bg-surface2/50 p-1">
        {allowed.users && <TabsTrigger value="users" className="gap-1.5"><Users className="h-3.5 w-3.5" /> Users</TabsTrigger>}
        {allowed.roles && <TabsTrigger value="roles" className="gap-1.5"><ShieldCheck className="h-3.5 w-3.5" /> Roles &amp; Permissions</TabsTrigger>}
        {allowed.auth && <TabsTrigger value="auth" className="gap-1.5"><KeyRound className="h-3.5 w-3.5" /> Authentication</TabsTrigger>}
      </TabsList>

      {allowed.users && <TabsContent value="users"><UsersSection /></TabsContent>}
      {allowed.roles && <TabsContent value="roles"><RolesSection /></TabsContent>}
      {allowed.auth && <TabsContent value="auth"><AuthProvidersSection /></TabsContent>}
    </Tabs>
  )
}
