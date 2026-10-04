import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Loader2, Lock, Pencil, Plus, ShieldAlert, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { isElevated, riskOf, type Catalog } from '@/lib/access'
import type { Role } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Skeleton } from '@/components/ui/Skeleton'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import { useCan } from '@/stores/auth'
import { RoleEditorDialog } from './RoleEditorDialog'
import { RoleViewDialog } from './RoleViewDialog'

type Dependencies = { users: number; provider_references: { provider_label: string; kind: string; match: string }[] }

export function RolesSection() {
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('roles.manage')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<Role | null>(null)
  const [template, setTemplate] = useState<Role | null>(null) // duplicate source
  const [deleting, setDeleting] = useState<Role | null>(null)
  const [viewing, setViewing] = useState<Role | null>(null)

  const rolesQ = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })
  const catalogQ = useQuery<Catalog>({
    queryKey: ['roles', 'catalog'],
    queryFn: async () => (await api.get('/roles/catalog')).data,
  })
  const roles = rolesQ.data
  const catalog = catalogQ.data
  const superPerm = catalog?.superuser_permission || 'system.admin'

  function openCreate(from?: Role) { setEditing(null); setTemplate(from || null); setEditorOpen(true) }
  function openEdit(r: Role) { setEditing(r); setTemplate(null); setEditorOpen(true) }

  const custom = (roles || []).filter((r) => !r.is_system).length
  const colSpan = canManage ? 6 : 5

  // Keep the dialog's role in step with the list after a save (revision!).
  const fresh = (r: Role | null) => (r && roles?.find((x) => x.id === r.id)) || r

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">
          {roles && <>{roles.length} roles · {custom} custom. </>}
          Built-in roles are read-only; duplicate one to start a custom role. You can only grant permissions you hold yourself.
        </p>
        {canManage && (
          <Button onClick={() => openCreate()} disabled={!catalog} title={!catalog ? 'The permission catalog has not loaded' : undefined}>
            <Plus className="h-4 w-4" /> New role
          </Button>
        )}
      </div>

      {catalogQ.isError && !catalog && (
        <QueryFailure compact error={catalogQ.error} what="the permission catalog" onRetry={() => void catalogQ.refetch()} retrying={catalogQ.isFetching} />
      )}
      {rolesQ.isError && (
        <QueryFailure error={rolesQ.error} what="roles" compact={!!roles} onRetry={() => void rolesQ.refetch()} retrying={rolesQ.isFetching} />
      )}

      {!(rolesQ.isError && !roles) && (
        <Card>
          <CardContent className="p-0">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Role</Th>
                  <Th>Description</Th>
                  <Th>Access</Th>
                  <Th className="text-right">Users</Th>
                  <Th>Type</Th>
                  {canManage && <Th className="w-28 text-right">Actions</Th>}
                </Tr>
              </THead>
              <TBody>
                {rolesQ.isLoading && Array.from({ length: 4 }).map((_, i) => (
                  <Tr key={i}><Td colSpan={colSpan} className="py-2.5"><Skeleton className="h-8 w-full" /></Td></Tr>
                ))}
                {roles?.map((r) => {
                  const full = r.permissions.includes(superPerm)
                  const standard = r.permissions.filter((p) => !isElevated(riskOf(catalog, p))).length
                  const elevated = r.permissions.length - standard
                  return (
                    <Tr key={r.id} className="cursor-pointer" onClick={() => setViewing(r)}>
                      <Td>
                        <button
                          type="button"
                          className="text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
                          aria-label={`View role ${r.display_name}`}
                          onClick={(e) => { e.stopPropagation(); setViewing(r) }}
                        >
                          <span className="block text-sm font-medium">{r.display_name}</span>
                          <span className="block font-mono text-[11px] text-muted">{r.name}</span>
                        </button>
                      </Td>
                      <Td className="max-w-md text-xs text-muted">{r.description || <span className="italic">No description</span>}</Td>
                      <Td>
                        {full ? (
                          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-warning">
                            <ShieldAlert className="h-3.5 w-3.5" aria-hidden /> Full administration
                          </span>
                        ) : (
                          <div className="text-xs">
                            <span className="tabular-nums">{r.permissions.length}</span> permission{r.permissions.length === 1 ? '' : 's'}
                            {elevated > 0 && <span className="ml-1.5 text-warning"><ShieldAlert className="mr-0.5 inline h-3 w-3" aria-hidden />{elevated} privileged</span>}
                          </div>
                        )}
                      </Td>
                      <Td className="text-right text-sm tabular-nums">{r.user_count}</Td>
                      <Td>
                        {r.is_system
                          ? <Badge variant="outline"><Lock className="h-3 w-3" /> Built-in</Badge>
                          : <Badge variant="info">Custom</Badge>}
                      </Td>
                      {canManage && (
                        <Td onClick={(e) => e.stopPropagation()}>
                          <div className="flex justify-end gap-0.5">
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" aria-label={`Duplicate ${r.display_name}`} onClick={() => openCreate(r)} disabled={!catalog}><Copy className="h-3.5 w-3.5" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7" title={r.is_system ? 'Built-in roles cannot be edited' : 'Edit'} aria-label={`Edit ${r.display_name}`} disabled={r.is_system || !catalog} onClick={() => openEdit(r)}><Pencil className="h-3.5 w-3.5" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                              title={r.is_system ? 'Built-in roles cannot be deleted' : r.user_count ? 'Reassign its users first' : 'Delete'}
                              aria-label={`Delete ${r.display_name}`}
                              disabled={r.is_system || r.user_count > 0} onClick={() => setDeleting(r)}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </Td>
                      )}
                    </Tr>
                  )
                })}
                {roles && roles.length === 0 && (
                  <Tr><Td colSpan={colSpan} className="py-8 text-center text-muted">No roles are defined.</Td></Tr>
                )}
              </TBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <RoleEditorDialog
        open={editorOpen}
        onOpenChange={setEditorOpen}
        role={fresh(editing)}
        template={template}
        catalog={catalog}
      />

      <RoleViewDialog
        role={fresh(viewing)}
        onClose={() => setViewing(null)}
        catalog={catalog}
        onDuplicate={canManage && catalog ? (r) => { setViewing(null); openCreate(r) } : undefined}
        onEdit={canManage && catalog ? (r) => { setViewing(null); openEdit(r) } : undefined}
      />

      <DeleteRoleDialog role={deleting} onClose={() => setDeleting(null)} onDeleted={() => { qc.invalidateQueries({ queryKey: ['roles'] }); setDeleting(null) }} />
    </div>
  )
}

/** Shows what still depends on a role before it can be deleted. */
function DeleteRoleDialog({ role, onClose, onDeleted }: { role: Role | null; onClose: () => void; onDeleted: () => void }) {
  const deps = useQuery<Dependencies>({
    queryKey: ['roles', role?.id, 'dependencies'],
    queryFn: async () => (await api.get(`/roles/${role!.id}/dependencies`)).data,
    enabled: !!role,
    staleTime: 0,
  })
  const del = useMutation({
    mutationFn: async () => api.delete(`/roles/${role!.id}`),
    onSuccess: () => { toast.success('Role deleted'); onDeleted() },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const blocked = !!deps.data && (deps.data.users > 0 || deps.data.provider_references.length > 0)
  const refs = useMemo(() => deps.data?.provider_references || [], [deps.data])
  return (
    <Dialog open={!!role} onOpenChange={(o) => !o && !del.isPending && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Delete role</DialogTitle>
          <DialogDescription>Delete <span className="font-semibold text-text">{role?.display_name}</span>? This cannot be undone.</DialogDescription>
        </DialogHeader>
        {deps.isLoading && <div className="flex items-center gap-2 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking what still uses it…</div>}
        {deps.isError && <QueryFailure compact error={deps.error} what="role usage" onRetry={() => void deps.refetch()} />}
        {deps.data && deps.data.users > 0 && <p role="alert" className="text-sm text-danger">{deps.data.users} {deps.data.users === 1 ? 'account still has' : 'accounts still have'} this role. Reassign them first.</p>}
        {refs.length > 0 && (
          <div role="alert" className="text-sm text-danger">
            Directory sign-in still maps to this role:
            <ul className="mt-1 list-disc pl-5 text-xs">{refs.map((r, i) => <li key={i}>{r.provider_label}: {r.kind === 'default_role' ? 'default role' : `rule “${r.match}”`}</li>)}</ul>
            <span className="text-xs text-muted">Point those rules at another role in Settings → Users &amp; Access → Authentication.</span>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={del.isPending}>Cancel</Button>
          <Button variant="destructive" disabled={!deps.data || blocked || del.isPending} onClick={() => del.mutate()}>
            {del.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
