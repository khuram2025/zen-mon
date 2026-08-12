import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pencil, Plus, RotateCw, Search, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import type { Role, User } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { UserFormDialog } from '@/components/forms/UserFormDialog'
import { toast } from '@/components/ui/Toast'
import { useCan } from '@/stores/auth'

const SOURCE_BADGE: Record<string, { label: string; variant: 'outline' | 'info' | 'warning' }> = {
  local: { label: 'Local', variant: 'outline' },
  ldap: { label: 'LDAP', variant: 'info' },
  radius: { label: 'RADIUS', variant: 'warning' },
}

export function UsersSection() {
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('users.manage')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [deleting, setDeleting] = useState<User | null>(null)
  const [resetTarget, setResetTarget] = useState<User | null>(null)
  const [newPass, setNewPass] = useState('')
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('all')

  const { data: users } = useQuery<User[]>({
    queryKey: ['users'],
    queryFn: async () => (await api.get('/users')).data,
  })
  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })

  const roleLabel = useMemo(() => {
    const map: Record<string, string> = {}
    for (const r of roles || []) map[r.name] = r.display_name
    return map
  }, [roles])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (users || []).filter((u) => {
      if (roleFilter !== 'all' && u.role !== roleFilter) return false
      if (!q) return true
      return [u.username, u.email, u.full_name || ''].some((s) => s.toLowerCase().includes(q))
    })
  }, [users, search, roleFilter])

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/${id}`),
    onSuccess: () => { toast.success('User deleted'); qc.invalidateQueries({ queryKey: ['users'] }); qc.invalidateQueries({ queryKey: ['roles'] }); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const resetPw = useMutation({
    mutationFn: async () => api.post(`/users/${resetTarget!.id}/reset-password`, { new_password: newPass }),
    onSuccess: () => { toast.success('Password reset'); setResetTarget(null); setNewPass('') },
    onError: (e: any) => toast.error('Reset failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex flex-1 flex-wrap items-end gap-2">
          <div className="relative w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="pl-8" placeholder="Search users…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <Select value={roleFilter} onValueChange={setRoleFilter}>
            <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All roles</SelectItem>
              {(roles || []).map((r) => (
                <SelectItem key={r.name} value={r.name}>{r.display_name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {canManage && (
          <Button onClick={() => { setEditing(null); setFormOpen(true) }}><Plus className="h-4 w-4" /> New user</Button>
        )}
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Username</Th><Th>Email</Th><Th>Role</Th><Th>Source</Th><Th>Active</Th><Th>Last login</Th>
                {canManage && <Th className="w-32 text-right">Actions</Th>}
              </Tr>
            </THead>
            <TBody>
              {filtered.map((u) => {
                const src = SOURCE_BADGE[u.auth_source || 'local'] || SOURCE_BADGE.local
                return (
                  <Tr key={u.id}>
                    <Td className="font-medium">
                      {u.username}
                      {u.full_name && <div className="text-[11px] font-normal text-muted">{u.full_name}</div>}
                    </Td>
                    <Td className="text-sm">{u.email}</Td>
                    <Td><Badge variant={u.role === 'admin' ? 'success' : 'info'}>{roleLabel[u.role] || u.role}</Badge></Td>
                    <Td><Badge variant={src.variant}>{src.label}</Badge></Td>
                    <Td><Badge variant={u.is_active ? 'success' : 'outline'}>{u.is_active ? 'yes' : 'no'}</Badge></Td>
                    <Td className="text-xs text-muted">{relativeTime(u.last_login)}</Td>
                    {canManage && (
                      <Td>
                        <div className="flex justify-end gap-0.5">
                          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(u); setFormOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                          {(u.auth_source || 'local') === 'local' && (
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="Reset password" onClick={() => setResetTarget(u)}><RotateCw className="h-3.5 w-3.5" /></Button>
                          )}
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" onClick={() => setDeleting(u)}><Trash2 className="h-3.5 w-3.5" /></Button>
                        </div>
                      </Td>
                    )}
                  </Tr>
                )
              })}
              {filtered.length === 0 && <Tr><Td colSpan={canManage ? 7 : 6} className="py-8 text-center text-muted">No users</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <UserFormDialog open={formOpen} onOpenChange={setFormOpen} user={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete user"
        description={<>Delete <span className="font-semibold text-text">{deleting?.username}</span>?</>}
        confirmText="Delete" destructive loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
      <Dialog open={!!resetTarget} onOpenChange={(o) => !o && setResetTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader><DialogTitle>Reset password</DialogTitle></DialogHeader>
          <FormField label="New password" required hint="Minimum 6 characters">
            <Input type="password" minLength={6} value={newPass} onChange={(e) => setNewPass(e.target.value)} autoFocus />
          </FormField>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetTarget(null)}>Cancel</Button>
            <Button disabled={newPass.length < 6 || resetPw.isPending} onClick={() => resetPw.mutate()}>
              {resetPw.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Reset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
