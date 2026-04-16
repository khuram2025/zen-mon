import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pencil, Plus, RotateCw, Trash2, Users } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { UserFormDialog } from '@/components/forms/UserFormDialog'
import { toast } from '@/components/ui/Toast'

export function UsersPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)
  const [resetTarget, setResetTarget] = useState<any>(null)
  const [newPass, setNewPass] = useState('')

  const { data: users } = useQuery<any[]>({
    queryKey: ['users'],
    queryFn: async () => (await api.get('/users')).data,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/${id}`),
    onSuccess: () => { toast.success('User deleted'); qc.invalidateQueries({ queryKey: ['users'] }); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const resetPw = useMutation({
    mutationFn: async () => api.post(`/users/${resetTarget.id}/reset-password`, { new_password: newPass }),
    onSuccess: () => { toast.success('Password reset'); setResetTarget(null); setNewPass('') },
    onError: (e: any) => toast.error('Reset failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Users className="h-5 w-5 text-primary" /> Users
          </h1>
          <p className="text-xs text-muted">Manage user accounts and access control</p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}><Plus className="h-4 w-4" /> New user</Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>Username</Th><Th>Email</Th><Th>Role</Th><Th>Active</Th><Th>Last login</Th><Th className="w-32 text-right">Actions</Th></Tr>
            </THead>
            <TBody>
              {(users || []).map((u) => (
                <Tr key={u.id}>
                  <Td className="font-medium">{u.username}</Td>
                  <Td className="text-sm">{u.email}</Td>
                  <Td><Badge variant={u.role === 'admin' ? 'success' : 'info'}>{u.role}</Badge></Td>
                  <Td><Badge variant={u.is_active ? 'success' : 'outline'}>{u.is_active ? 'yes' : 'no'}</Badge></Td>
                  <Td className="text-xs text-muted">{relativeTime(u.last_login)}</Td>
                  <Td>
                    <div className="flex justify-end gap-0.5">
                      <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(u); setFormOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                      <Button variant="ghost" size="icon" className="h-7 w-7" title="Reset password" onClick={() => setResetTarget(u)}><RotateCw className="h-3.5 w-3.5" /></Button>
                      <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" onClick={() => setDeleting(u)}><Trash2 className="h-3.5 w-3.5" /></Button>
                    </div>
                  </Td>
                </Tr>
              ))}
              {(!users || users.length === 0) && <Tr><Td colSpan={6} className="py-8 text-center text-muted">No users</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <UserFormDialog open={formOpen} onOpenChange={setFormOpen} user={editing} />
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} title="Delete user" description={<>Delete <span className="font-semibold text-text">{deleting?.username}</span>?</>} confirmText="Delete" destructive loading={del.isPending} onConfirm={() => { if (deleting) del.mutate(deleting.id) }} />
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
