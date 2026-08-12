import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Loader2, Lock, Pencil, Plus, ShieldAlert, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { PermissionCatalogModule, Role } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { useCan } from '@/stores/auth'

type Catalog = { modules: PermissionCatalogModule[]; superuser_permission: string }

export function RolesSection() {
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('roles.manage')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<Role | null>(null)
  const [template, setTemplate] = useState<Role | null>(null) // duplicate source
  const [deleting, setDeleting] = useState<Role | null>(null)
  const [viewing, setViewing] = useState<Role | null>(null)

  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })
  const { data: catalog } = useQuery<Catalog>({
    queryKey: ['roles', 'catalog'],
    queryFn: async () => (await api.get('/roles/catalog')).data,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/roles/${id}`),
    onSuccess: () => { toast.success('Role deleted'); qc.invalidateQueries({ queryKey: ['roles'] }); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  function openCreate(from?: Role) {
    setEditing(null)
    setTemplate(from || null)
    setEditorOpen(true)
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">
          Roles bundle per-module permissions. Built-in roles are locked — duplicate one to customize it.
        </p>
        {canManage && (
          <Button onClick={() => openCreate()}><Plus className="h-4 w-4" /> New role</Button>
        )}
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Role</Th><Th>Description</Th><Th>Permissions</Th><Th>Users</Th><Th>Type</Th>
                {canManage && <Th className="w-28 text-right">Actions</Th>}
              </Tr>
            </THead>
            <TBody>
              {(roles || []).map((r) => (
                <Tr key={r.id}>
                  <Td className="font-medium">
                    {r.display_name}
                    <div className="text-[11px] font-normal text-muted">{r.name}</div>
                  </Td>
                  <Td className="max-w-md text-xs text-muted">{r.description}</Td>
                  <Td>
                    <button className="text-xs text-primary hover:underline" onClick={() => setViewing(r)}>
                      {r.permissions.includes('system.admin') ? 'Full access' : `${r.permissions.length} permissions`}
                    </button>
                  </Td>
                  <Td><Badge variant={r.user_count ? 'info' : 'outline'}>{r.user_count}</Badge></Td>
                  <Td>
                    {r.is_system
                      ? <Badge variant="outline"><Lock className="mr-1 h-3 w-3" /> Built-in</Badge>
                      : <Badge variant="success">Custom</Badge>}
                  </Td>
                  {canManage && (
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" onClick={() => openCreate(r)}><Copy className="h-3.5 w-3.5" /></Button>
                        {!r.is_system && (
                          <>
                            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(r); setTemplate(null); setEditorOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" onClick={() => setDeleting(r)}><Trash2 className="h-3.5 w-3.5" /></Button>
                          </>
                        )}
                      </div>
                    </Td>
                  )}
                </Tr>
              ))}
              {(!roles || roles.length === 0) && <Tr><Td colSpan={canManage ? 6 : 5} className="py-8 text-center text-muted">No roles</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <RoleEditorDialog
        open={editorOpen}
        onOpenChange={setEditorOpen}
        role={editing}
        template={template}
        catalog={catalog}
        canGrantSuperuser={can('system.admin')}
      />

      <PermissionsViewDialog role={viewing} onClose={() => setViewing(null)} catalog={catalog} />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete role"
        description={<>Delete role <span className="font-semibold text-text">{deleting?.display_name}</span>?</>}
        confirmText="Delete" destructive loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

function PermissionsViewDialog({ role, onClose, catalog }: { role: Role | null; onClose: () => void; catalog?: Catalog }) {
  const granted = new Set(role?.permissions || [])
  const full = granted.has('system.admin')
  return (
    <Dialog open={!!role} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>{role?.display_name} — permissions</DialogTitle></DialogHeader>
        <div className="max-h-[60vh] space-y-3 overflow-y-auto pr-1">
          {(catalog?.modules || []).map((m) => {
            const hits = m.permissions.filter((p) => full || granted.has(p.id))
            if (!hits.length) return null
            return (
              <div key={m.module}>
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{m.label}</div>
                <div className="flex flex-wrap gap-1">
                  {hits.map((p) => <Badge key={p.id} variant="info">{p.label}</Badge>)}
                </div>
              </div>
            )
          })}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function RoleEditorDialog({
  open, onOpenChange, role, template, catalog, canGrantSuperuser,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  role: Role | null            // editing an existing custom role
  template: Role | null        // duplicating: pre-fill from this role
  catalog?: Catalog
  canGrantSuperuser: boolean
}) {
  const qc = useQueryClient()
  const isEdit = !!role
  const [displayName, setDisplayName] = useState('')
  const [name, setName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [description, setDescription] = useState('')
  const [granted, setGranted] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!open) return
    const src = role || template
    setDisplayName(role ? role.display_name : template ? `${template.display_name} (copy)` : '')
    setName(role ? role.name : '')
    setNameTouched(!!role)
    setDescription(src?.description || '')
    setGranted(new Set(src?.permissions || []))
  }, [open, role, template])

  const slug = useMemo(
    () => (nameTouched ? name : displayName.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 50)),
    [name, nameTouched, displayName],
  )

  const superPerm = catalog?.superuser_permission || 'system.admin'
  const isFullAdmin = granted.has(superPerm)

  function toggle(id: string) {
    setGranted((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleModule(m: PermissionCatalogModule, on: boolean) {
    setGranted((prev) => {
      const next = new Set(prev)
      for (const p of m.permissions) {
        if (on) next.add(p.id)
        else next.delete(p.id)
      }
      return next
    })
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        name: slug || undefined,
        display_name: displayName,
        description,
        permissions: Array.from(granted),
      }
      if (isEdit) return (await api.put(`/roles/${role!.id}`, payload)).data
      return (await api.post('/roles', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Role updated' : 'Role created')
      qc.invalidateQueries({ queryKey: ['roles'] })
      qc.invalidateQueries({ queryKey: ['users'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>{isEdit ? `Edit role: ${role?.display_name}` : 'New role'}</DialogTitle></DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Display name" required>
              <Input required minLength={2} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Helpdesk" />
            </FormField>
            <FormField label="Identifier" hint="Lowercase letters, digits, _ or -">
              <Input value={slug} onChange={(e) => { setName(e.target.value); setNameTouched(true) }} placeholder="helpdesk" />
            </FormField>
          </div>
          <FormField label="Description">
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this role is for…" />
          </FormField>

          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Permissions</div>
            <div className="max-h-[42vh] space-y-2 overflow-y-auto rounded-md border border-border p-3">
              {(catalog?.modules || []).map((m) => {
                const isSystemModule = m.permissions.some((p) => p.id === superPerm)
                const allOn = m.permissions.every((p) => granted.has(p.id))
                return (
                  <div key={m.module} className="rounded-md bg-surface2/30 p-2">
                    <label className="flex cursor-pointer items-center gap-2">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 accent-primary"
                        checked={allOn}
                        onChange={(e) => toggleModule(m, e.target.checked)}
                        disabled={isSystemModule && !canGrantSuperuser}
                      />
                      <span className="text-sm font-medium">{m.label}</span>
                      {isSystemModule && <ShieldAlert className="h-3.5 w-3.5 text-warning" />}
                    </label>
                    <div className="ml-5 mt-1 grid grid-cols-1 gap-1 md:grid-cols-2">
                      {m.permissions.map((p) => (
                        <label key={p.id} className="flex cursor-pointer items-start gap-2" title={p.description}>
                          <input
                            type="checkbox"
                            className="mt-0.5 h-3.5 w-3.5 accent-primary"
                            checked={granted.has(p.id) || (isFullAdmin && p.id !== superPerm)}
                            disabled={(isFullAdmin && p.id !== superPerm) || (p.id === superPerm && !canGrantSuperuser)}
                            onChange={() => toggle(p.id)}
                          />
                          <span className="text-xs">
                            {p.label}
                            <span className="block text-[10px] leading-tight text-muted">{p.description}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
            {isFullAdmin && (
              <p className="flex items-center gap-1.5 text-[11px] text-warning">
                <ShieldAlert className="h-3.5 w-3.5" /> Full administration grants every permission, including user, security, and system management.
              </p>
            )}
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || !displayName.trim()}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create role'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
