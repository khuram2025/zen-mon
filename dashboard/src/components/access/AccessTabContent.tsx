import { useSearchParams } from 'react-router-dom'
import { KeyRound, ShieldCheck, Users } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useCan } from '@/stores/auth'
import { UsersSection } from './UsersSection'
import { RolesSection } from './RolesSection'
import { AuthProvidersSection } from './AuthProvidersSection'

/** Settings → Users & Access: user accounts, roles & permissions, and
 * LDAP/RADIUS authentication providers. Sub-tab kept in the `sub` URL
 * param so links deep into a section survive refresh. */
export function AccessTabContent() {
  const can = useCan()
  const [searchParams, setSearchParams] = useSearchParams()
  const showAuth = can('system.admin')

  const requested = searchParams.get('sub')
  const active = requested === 'roles' ? 'roles' : requested === 'auth' && showAuth ? 'auth' : 'users'

  function setActive(v: string) {
    const next = new URLSearchParams(searchParams)
    if (v === 'users') next.delete('sub')
    else next.set('sub', v)
    setSearchParams(next, { replace: true })
  }

  if (!can('users.view') && !can('users.manage')) {
    return (
      <div className="rounded-md border border-border bg-surface2/40 p-6 text-sm text-muted">
        Your role does not include user management permissions.
      </div>
    )
  }

  return (
    <Tabs value={active} onValueChange={setActive}>
      <TabsList className="mb-1 bg-surface2/50 p-1">
        <TabsTrigger value="users" className="gap-1.5">
          <Users className="h-3.5 w-3.5" /> Users
        </TabsTrigger>
        <TabsTrigger value="roles" className="gap-1.5">
          <ShieldCheck className="h-3.5 w-3.5" /> Roles & Permissions
        </TabsTrigger>
        {showAuth && (
          <TabsTrigger value="auth" className="gap-1.5">
            <KeyRound className="h-3.5 w-3.5" /> Authentication
          </TabsTrigger>
        )}
      </TabsList>

      <TabsContent value="users"><UsersSection /></TabsContent>
      <TabsContent value="roles"><RolesSection /></TabsContent>
      {showAuth && <TabsContent value="auth"><AuthProvidersSection /></TabsContent>}
    </Tabs>
  )
}
