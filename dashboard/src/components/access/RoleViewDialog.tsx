import { useQuery } from '@tanstack/react-query'
import { Copy, Lock, Pencil, ShieldAlert, Users as UsersIcon } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import type { Role } from '@/types'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { QueryBoundary } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { useCanAny } from '@/stores/auth'
import type { Catalog } from '@/lib/access'
import { PermissionMatrix } from './PermissionMatrix'

type Member = { id: string; username: string; full_name: string | null; email: string; is_active: boolean; auth_source: string; scope_tags: string[]; last_login: string | null; mfa_enabled: boolean }
type HistoryRow = { revision: number; action: string; actor: string | null; at: string; added: string[]; removed: string[]; affected_users: number; reason: string | null }

const ACTION_LABEL: Record<string, string> = { create: 'Created', update: 'Updated', rename: 'Renamed', delete: 'Deleted', builtin_reconcile: 'Built-in definition updated' }

/** Read-only role detail: what it grants, who has it, and how it changed. */
export function RoleViewDialog({ role, onClose, catalog, onDuplicate, onEdit }: {
  role: Role | null
  onClose: () => void
  catalog?: Catalog
  onDuplicate?: (r: Role) => void
  onEdit?: (r: Role) => void
}) {
  const canAny = useCanAny()
  const superPerm = catalog?.superuser_permission || 'system.admin'
  const granted = new Set(role?.permissions || [])
  const canMembers = canAny(['roles.view', 'roles.manage', 'users.view', 'users.manage'])
  const canHistory = canAny(['roles.view', 'roles.manage', 'audit.view'])

  const members = useQuery<{ total: number; data: Member[] }>({
    queryKey: ['roles', role?.id, 'members'],
    queryFn: async () => (await api.get(`/roles/${role!.id}/members`)).data,
    enabled: !!role && canMembers,
  })
  const history = useQuery<{ data: HistoryRow[] }>({
    queryKey: ['roles', role?.id, 'history'],
    queryFn: async () => (await api.get(`/roles/${role!.id}/history`)).data,
    enabled: !!role && canHistory,
  })

  return (
    <Dialog open={!!role} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            {role?.display_name}
            {role?.is_system ? <Badge variant="outline"><Lock className="h-3 w-3" /> Built-in</Badge> : <Badge variant="info">Custom</Badge>}
            {role?.privileged && !granted.has(superPerm) && <Badge variant="warning"><ShieldAlert className="h-3 w-3" /> Can manage access</Badge>}
          </DialogTitle>
          <DialogDescription>
            <span className="font-mono text-xs">{role?.name}</span>
            {role?.description && <> · {role.description}</>}
            {role?.updated_by && <> · last changed by {role.updated_by} {relativeTime(role.updated_at || null)}</>}
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="permissions">
          <TabsList className="bg-surface2/50 p-1">
            <TabsTrigger value="permissions" className="text-xs">Permissions</TabsTrigger>
            {canMembers && <TabsTrigger value="members" className="text-xs">Members{role ? ` (${role.user_count})` : ''}</TabsTrigger>}
            {canHistory && <TabsTrigger value="history" className="text-xs">History</TabsTrigger>}
          </TabsList>

          <TabsContent value="permissions" className="mt-3">
            {granted.has(superPerm) && (
              <p className="mb-2 flex items-center gap-1.5 text-xs text-warning">
                <ShieldAlert className="h-3.5 w-3.5" /> Full administration: every permission below is implied and visibility scope is ignored.
              </p>
            )}
            <PermissionMatrix catalog={catalog} granted={granted} superPerm={superPerm} readOnly />
          </TabsContent>

          <TabsContent value="members" className="mt-3">
            <QueryBoundary query={members} what="role members" skeleton={<Skeleton className="h-24 w-full" />}>
              {(m) => m.data.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted">No account has this role.</p>
              ) : (
                <div className="space-y-2">
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {m.data.map((u) => (
                      <li key={u.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                        <div className="min-w-0">
                          <div className="truncate font-medium">{u.full_name || u.username}</div>
                          <div className="truncate text-[11px] text-muted">{u.username} · {u.email}</div>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                          <Badge variant={u.is_active ? 'success' : 'outline'}>{u.is_active ? 'Active' : 'Disabled'}</Badge>
                          {u.auth_source !== 'local' && <Badge variant="info">{u.auth_source.toUpperCase()}</Badge>}
                          {u.mfa_enabled && <Badge variant="outline">MFA</Badge>}
                          <span className="text-muted">{u.scope_tags.length ? `scope: ${u.scope_tags.join(', ')}` : 'all resources'}</span>
                          <span className="text-muted">signed in {relativeTime(u.last_login)}</span>
                        </div>
                      </li>
                    ))}
                  </ul>
                  <div className="flex items-center justify-between text-xs text-muted">
                    <span>{m.total > m.data.length ? `Showing ${m.data.length} of ${m.total}` : `${m.total} ${m.total === 1 ? 'account' : 'accounts'}`}</span>
                    <Link className="inline-flex items-center gap-1 text-primary hover:underline" to={`/settings/general?tab=users&role=${encodeURIComponent(role!.name)}`} onClick={onClose}>
                      <UsersIcon className="h-3 w-3" /> Open in Users
                    </Link>
                  </div>
                </div>
              )}
            </QueryBoundary>
          </TabsContent>

          <TabsContent value="history" className="mt-3">
            <QueryBoundary query={history} what="role history" skeleton={<Skeleton className="h-24 w-full" />}>
              {(h) => h.data.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted">No recorded changes for this role.</p>
              ) : (
                <ol className="space-y-2">
                  {h.data.map((r, i) => (
                    <li key={`${r.revision}-${i}`} className="rounded-md border border-border p-2.5 text-xs">
                      <div className="flex flex-wrap items-center justify-between gap-1">
                        <span className="font-medium">{ACTION_LABEL[r.action] || r.action} <span className="font-normal text-muted">· revision {r.revision}</span></span>
                        <span className="text-muted">{r.actor || 'system'} · <time dateTime={r.at}>{new Date(r.at).toLocaleString()}</time></span>
                      </div>
                      {(r.added.length > 0 || r.removed.length > 0) && (
                        <div className="mt-1.5 space-y-0.5 font-mono text-[11px]">
                          {r.added.length > 0 && <div className="text-success">+ {r.added.join(', ')}</div>}
                          {r.removed.length > 0 && <div className="text-danger">− {r.removed.join(', ')}</div>}
                        </div>
                      )}
                      <div className="mt-1 text-muted">{r.affected_users} {r.affected_users === 1 ? 'account' : 'accounts'} affected{r.reason ? <> · “{r.reason}”</> : null}</div>
                    </li>
                  ))}
                </ol>
              )}
            </QueryBoundary>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
          {onDuplicate && role && <Button variant="outline" onClick={() => onDuplicate(role)}><Copy className="h-4 w-4" /> Duplicate</Button>}
          {onEdit && role && !role.is_system && <Button onClick={() => onEdit(role)}><Pencil className="h-4 w-4" /> Edit</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
void cn
