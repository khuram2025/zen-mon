import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Loader2, Lock, Minus, Pencil, Plus, ShieldAlert, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import type { PermissionCatalogModule, Role } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Skeleton } from '@/components/ui/Skeleton'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
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

  const { data: roles, isLoading } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })
  const { data: catalog } = useQuery<Catalog>({
    queryKey: ['roles', 'catalog'],
    queryFn: async () => (await api.get('/roles/catalog')).data,
  })
  const superPerm = catalog?.superuser_permission || 'system.admin'
  const totalPerms = useMemo(() => (catalog?.modules || []).reduce((n, m) => n + m.permissions.length, 0), [catalog])

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
  function openEdit(r: Role) {
    setEditing(r)
    setTemplate(null)
    setEditorOpen(true)
  }

  const custom = (roles || []).filter((r) => !r.is_system).length

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">
          {!isLoading && <>{(roles || []).length} roles · {custom} custom. </>}
          Built-in roles are read-only; duplicate one to start a custom role.
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
                <Th>Role</Th>
                <Th>Description</Th>
                <Th>Access</Th>
                <Th className="text-right">Users</Th>
                <Th>Type</Th>
                {canManage && <Th className="w-28 text-right">Actions</Th>}
              </Tr>
            </THead>
            <TBody>
              {isLoading && Array.from({ length: 4 }).map((_, i) => (
                <Tr key={i}><Td colSpan={canManage ? 6 : 5} className="py-2.5"><Skeleton className="h-8 w-full" /></Td></Tr>
              ))}
              {!isLoading && (roles || []).map((r) => {
                const full = r.permissions.includes(superPerm)
                return (
                  <Tr key={r.id} className="cursor-pointer" onClick={() => setViewing(r)}>
                    <Td>
                      <div className="text-sm font-medium">{r.display_name}</div>
                      <div className="font-mono text-[11px] text-muted">{r.name}</div>
                    </Td>
                    <Td className="max-w-md text-xs text-muted">{r.description || <span className="italic">No description</span>}</Td>
                    <Td>
                      {full ? (
                        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-warning">
                          <ShieldAlert className="h-3.5 w-3.5" /> Full administration
                        </span>
                      ) : (
                        <AccessBar granted={r.permissions.length} total={totalPerms} />
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
                          <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" onClick={() => openCreate(r)}><Copy className="h-3.5 w-3.5" /></Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7" title={r.is_system ? 'Built-in roles cannot be edited' : 'Edit'} disabled={r.is_system} onClick={() => openEdit(r)}><Pencil className="h-3.5 w-3.5" /></Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                            title={r.is_system ? 'Built-in roles cannot be deleted' : r.user_count ? 'Reassign its users first' : 'Delete'}
                            disabled={r.is_system || r.user_count > 0} onClick={() => setDeleting(r)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </Td>
                    )}
                  </Tr>
                )
              })}
              {!isLoading && (!roles || roles.length === 0) && (
                <Tr><Td colSpan={canManage ? 6 : 5} className="py-8 text-center text-muted">No roles</Td></Tr>
              )}
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

      <PermissionsViewDialog
        role={viewing}
        onClose={() => setViewing(null)}
        catalog={catalog}
        onDuplicate={canManage ? (r) => { setViewing(null); openCreate(r) } : undefined}
        onEdit={canManage ? (r) => { setViewing(null); openEdit(r) } : undefined}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete role"
        description={<>Delete the role <span className="font-semibold text-text">{deleting?.display_name}</span>? This cannot be undone.</>}
        confirmText="Delete" destructive loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

function AccessBar({ granted, total }: { granted: number; total: number }) {
  const pct = total ? Math.round((granted / total) * 100) : 0
  return (
    <div className="flex items-center gap-2 text-xs">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-surface2">
        <div className="h-full rounded-full bg-primary/70" style={{ width: `${pct}%` }} />
      </div>
      <span className="tabular-nums text-muted">{granted} / {total}</span>
    </div>
  )
}

// ─── Permission matrix ──────────────────────────────────────────────────────

/** Module × permission grid used by both the read-only view and the editor. */
function PermissionMatrix({ catalog, granted, superPerm, readOnly, canGrantSuperuser, onToggle, onToggleModule }: {
  catalog?: Catalog
  granted: Set<string>
  superPerm: string
  readOnly?: boolean
  canGrantSuperuser?: boolean
  onToggle?: (id: string) => void
  onToggleModule?: (m: PermissionCatalogModule, on: boolean) => void
}) {
  const full = granted.has(superPerm)
  const modules = catalog?.modules || []
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-sm">
        <thead className="bg-surface2/50 text-[11px] uppercase tracking-wider text-muted">
          <tr>
            <th className="w-48 px-3 py-2 text-left font-medium">Module</th>
            <th className="px-3 py-2 text-left font-medium">Permissions</th>
            {!readOnly && <th className="w-16 px-3 py-2 text-center font-medium">All</th>}
          </tr>
        </thead>
        <tbody>
          {modules.map((m) => {
            const isSystemModule = m.permissions.some((p) => p.id === superPerm)
            const allOn = m.permissions.every((p) => granted.has(p.id))
            const anyOn = m.permissions.some((p) => granted.has(p.id))
            const moduleLocked = isSystemModule && !canGrantSuperuser
            return (
              <tr key={m.module} className={cn('border-t border-border', isSystemModule && 'bg-warning/[0.04]')}>
                <td className="px-3 py-2 align-top">
                  <div className="flex items-center gap-1.5 text-sm font-medium">
                    {m.label}
                    {isSystemModule && <ShieldAlert className="h-3.5 w-3.5 text-warning" />}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-x-4 gap-y-1.5">
                    {m.permissions.map((p) => {
                      const isSuper = p.id === superPerm
                      const implied = full && !isSuper
                      const on = granted.has(p.id) || implied
                      if (readOnly) {
                        return (
                          <span key={p.id} title={p.description}
                            className={cn('inline-flex items-center gap-1.5 text-xs', on ? 'text-text' : 'text-muted/60')}>
                            {on ? <Check className="h-3.5 w-3.5 text-success" /> : <Minus className="h-3.5 w-3.5" />}
                            {p.label}
                          </span>
                        )
                      }
                      const disabled = implied || (isSuper && !canGrantSuperuser)
                      return (
                        <label key={p.id} title={p.description}
                          className={cn('inline-flex items-center gap-1.5 text-xs', disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer')}>
                          <input
                            type="checkbox"
                            className="h-3.5 w-3.5 accent-primary"
                            checked={on}
                            disabled={disabled}
                            onChange={() => onToggle?.(p.id)}
                          />
                          {p.label}
                        </label>
                      )
                    })}
                  </div>
                </td>
                {!readOnly && (
                  <td className="px-3 py-2 text-center">
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 accent-primary"
                      checked={allOn || (full && !isSystemModule)}
                      ref={(el) => { if (el) el.indeterminate = !allOn && anyOn && !full }}
                      disabled={moduleLocked || (full && !isSystemModule)}
                      onChange={(e) => onToggleModule?.(m, e.target.checked)}
                      title={`Toggle all ${m.label} permissions`}
                    />
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function PermissionsViewDialog({ role, onClose, catalog, onDuplicate, onEdit }: {
  role: Role | null; onClose: () => void; catalog?: Catalog
  onDuplicate?: (r: Role) => void; onEdit?: (r: Role) => void
}) {
  const granted = new Set(role?.permissions || [])
  const superPerm = catalog?.superuser_permission || 'system.admin'
  return (
    <Dialog open={!!role} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {role?.display_name}
            {role?.is_system ? <Badge variant="outline"><Lock className="h-3 w-3" /> Built-in</Badge> : <Badge variant="info">Custom</Badge>}
          </DialogTitle>
          <DialogDescription>
            <span className="font-mono text-xs">{role?.name}</span>
            {role?.description && <> · {role.description}</>}
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-y-auto pr-1">
          {granted.has(superPerm) && (
            <p className="mb-2 flex items-center gap-1.5 text-xs text-warning">
              <ShieldAlert className="h-3.5 w-3.5" /> Full administration: every permission below is implied.
            </p>
          )}
          <PermissionMatrix catalog={catalog} granted={granted} superPerm={superPerm} readOnly />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
          {onDuplicate && role && <Button variant="outline" onClick={() => onDuplicate(role)}><Copy className="h-4 w-4" /> Duplicate</Button>}
          {onEdit && role && !role.is_system && <Button onClick={() => onEdit(role)}><Pencil className="h-4 w-4" /> Edit</Button>}
        </DialogFooter>
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
  const total = (catalog?.modules || []).reduce((n, m) => n + m.permissions.length, 0)

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
        if (p.id === superPerm && !canGrantSuperuser) continue
        if (on) next.add(p.id)
        else next.delete(p.id)
      }
      return next
    })
  }

  function setAll(on: boolean) {
    setGranted(() => {
      if (!on) return new Set()
      const next = new Set<string>()
      for (const m of catalog?.modules || []) for (const p of m.permissions) if (p.id !== superPerm) next.add(p.id)
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
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit role: ${role?.display_name}` : template ? `New role from ${template.display_name}` : 'New role'}</DialogTitle>
          <DialogDescription>
            {isEdit && role?.user_count
              ? `${role.user_count} ${role.user_count === 1 ? 'user has' : 'users have'} this role; changes apply on their next request.`
              : 'A role is a named set of permissions you assign to users.'}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Display name" required>
              <Input required minLength={2} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Helpdesk" autoFocus={!isEdit} />
            </FormField>
            <FormField label="Identifier" hint="Lowercase letters, digits, _ or -">
              <Input value={slug} onChange={(e) => { setName(e.target.value); setNameTouched(true) }} placeholder="helpdesk" className="font-mono text-xs" />
            </FormField>
          </div>
          <FormField label="Description">
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this role is for…" />
          </FormField>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">
                Permissions
                <span className="ml-2 font-normal normal-case tracking-normal">
                  {isFullAdmin ? 'full administration' : `${granted.size} of ${total} selected`}
                </span>
              </div>
              {!isFullAdmin && (
                <div className="flex gap-1">
                  <Button type="button" variant="ghost" size="sm" onClick={() => setAll(true)}>Select all</Button>
                  <Button type="button" variant="ghost" size="sm" onClick={() => setAll(false)}>Clear</Button>
                </div>
              )}
            </div>
            <div className="max-h-[46vh] overflow-y-auto">
              <PermissionMatrix
                catalog={catalog}
                granted={granted}
                superPerm={superPerm}
                canGrantSuperuser={canGrantSuperuser}
                onToggle={toggle}
                onToggleModule={toggleModule}
              />
            </div>
            {isFullAdmin && (
              <p className="flex items-center gap-1.5 text-[11px] text-warning">
                <ShieldAlert className="h-3.5 w-3.5" /> Full administration grants every permission, including user, security, and system management.
              </p>
            )}
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || displayName.trim().length < 2}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create role'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
