import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2, Pencil, Plus, Search, Trash2, UserX } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import type { Role, User } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { StatusDot } from '@/components/ui/StatusDot'
import { Skeleton } from '@/components/ui/Skeleton'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { UserFormDialog } from '@/components/forms/UserFormDialog'
import { toast } from '@/components/ui/Toast'
import { useAuth, useCan } from '@/stores/auth'
import { TagBadge } from '@/components/tags/TagBadge'
import { tagColor, tagColorMap, useTags } from '@/hooks/useTags'

const SOURCE_LABEL: Record<string, string> = { local: 'Local', ldap: 'LDAP / AD', radius: 'RADIUS' }

function initials(u: User) {
  const src = (u.full_name || u.username).trim()
  const parts = src.split(/\s+/).filter(Boolean)
  return (parts.length >= 2 ? parts[0][0] + parts[1][0] : src.slice(0, 2)).toUpperCase()
}

export function UsersSection() {
  const qc = useQueryClient()
  const can = useCan()
  const me = useAuth((s) => s.user)
  const canManage = can('users.manage')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [deleting, setDeleting] = useState<User | null>(null)
  const [resetTarget, setResetTarget] = useState<User | null>(null)
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'disabled'>('all')

  const { data: users, isLoading } = useQuery<User[]>({
    queryKey: ['users'],
    queryFn: async () => (await api.get('/users')).data,
  })
  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })
  const { data: tagDefs } = useTags()
  const tagColors = useMemo(() => tagColorMap(tagDefs), [tagDefs])

  const roleLabel = useMemo(() => {
    const map: Record<string, string> = {}
    for (const r of roles || []) map[r.name] = r.display_name
    return map
  }, [roles])
  const adminRoles = useMemo(
    () => new Set((roles || []).filter((r) => r.permissions.includes('system.admin')).map((r) => r.name)),
    [roles],
  )

  const sorted = useMemo(
    () => [...(users || [])].sort((a, b) => a.username.localeCompare(b.username)),
    [users],
  )
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return sorted.filter((u) => {
      if (roleFilter !== 'all' && u.role !== roleFilter) return false
      if (statusFilter === 'active' && u.is_active === false) return false
      if (statusFilter === 'disabled' && u.is_active !== false) return false
      if (!q) return true
      return [u.username, u.email, u.full_name || ''].some((s) => s.toLowerCase().includes(q))
    })
  }, [sorted, search, roleFilter, statusFilter])

  const stats = useMemo(() => {
    const all = users || []
    return {
      total: all.length,
      disabled: all.filter((u) => u.is_active === false).length,
      external: all.filter((u) => (u.auth_source || 'local') !== 'local').length,
    }
  }, [users])

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/${id}`),
    onSuccess: () => { toast.success('User deleted'); qc.invalidateQueries({ queryKey: ['users'] }); qc.invalidateQueries({ queryKey: ['roles'] }); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const filtering = !!search.trim() || roleFilter !== 'all' || statusFilter !== 'all'

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-1 flex-wrap items-center gap-2">
          <div className="relative w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="pl-8" placeholder="Search name, username, email…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <Select value={roleFilter} onValueChange={setRoleFilter}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All roles</SelectItem>
              {(roles || []).map((r) => (
                <SelectItem key={r.name} value={r.name}>{r.display_name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as any)}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any status</SelectItem>
              <SelectItem value="active">Active</SelectItem>
              <SelectItem value="disabled">Disabled</SelectItem>
            </SelectContent>
          </Select>
          {!isLoading && (
            <span className="text-xs text-muted">
              {filtering ? `${filtered.length} of ${stats.total}` : stats.total} {stats.total === 1 ? 'user' : 'users'}
              {stats.disabled > 0 && <> · {stats.disabled} disabled</>}
              {stats.external > 0 && <> · {stats.external} directory</>}
            </span>
          )}
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
                <Th>User</Th>
                <Th>Role</Th>
                <Th>Visibility</Th>
                <Th>Source</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
                {canManage && <Th className="w-28 text-right">Actions</Th>}
              </Tr>
            </THead>
            <TBody>
              {isLoading && Array.from({ length: 3 }).map((_, i) => (
                <Tr key={i}>
                  <Td colSpan={canManage ? 7 : 6} className="py-2.5"><Skeleton className="h-8 w-full" /></Td>
                </Tr>
              ))}
              {!isLoading && filtered.map((u) => {
                const isMe = me?.id === u.id
                const external = (u.auth_source || 'local') !== 'local'
                const active = u.is_active !== false
                const isAdmin = adminRoles.has(u.role)
                return (
                  <Tr
                    key={u.id}
                    className={cn(canManage && 'cursor-pointer', !active && 'opacity-70')}
                    onClick={() => { if (canManage) { setEditing(u); setFormOpen(true) } }}
                  >
                    <Td>
                      <div className="flex items-center gap-3">
                        <div className={cn(
                          'flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold',
                          isAdmin ? 'bg-primary/15 text-primary' : 'bg-surface2 text-muted',
                        )}>
                          {initials(u)}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5 text-sm font-medium">
                            <span className="truncate">{u.full_name || u.username}</span>
                            {isMe && <Badge variant="outline" className="px-1.5 py-0 text-[10px]">You</Badge>}
                          </div>
                          <div className="truncate text-[11px] text-muted">
                            {u.full_name ? <>{u.username} · </> : null}{u.email}
                          </div>
                        </div>
                      </div>
                    </Td>
                    <Td><Badge variant={isAdmin ? 'success' : 'info'}>{roleLabel[u.role] || u.role}</Badge></Td>
                    <Td>
                      {(u.scope_tags || []).length === 0 || isAdmin ? (
                        <span className="text-xs text-muted">Everything</span>
                      ) : (
                        <div className="flex max-w-[220px] flex-wrap gap-1">
                          {(u.scope_tags || []).slice(0, 3).map((t) => (
                            <TagBadge key={t} name={t} color={tagColor(t, tagColors)} />
                          ))}
                          {(u.scope_tags || []).length > 3 && (
                            <span className="text-[10px] text-muted">+{(u.scope_tags || []).length - 3}</span>
                          )}
                        </div>
                      )}
                    </Td>
                    <Td className="text-xs">{SOURCE_LABEL[u.auth_source || 'local'] || u.auth_source}</Td>
                    <Td>
                      <span className="inline-flex items-center gap-2 text-xs">
                        <StatusDot status={active ? 'up' : 'idle'} /> {active ? 'Active' : 'Disabled'}
                      </span>
                    </Td>
                    <Td className="text-xs text-muted">{relativeTime(u.last_login)}</Td>
                    {canManage && (
                      <Td onClick={(e) => e.stopPropagation()}>
                        <div className="flex justify-end gap-0.5">
                          <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" onClick={() => { setEditing(u); setFormOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7" title={external ? 'Password is managed by the directory' : 'Reset password'} disabled={external} onClick={() => setResetTarget(u)}><KeyRound className="h-3.5 w-3.5" /></Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title={isMe ? 'You cannot delete your own account' : 'Delete'} disabled={isMe} onClick={() => setDeleting(u)}><Trash2 className="h-3.5 w-3.5" /></Button>
                        </div>
                      </Td>
                    )}
                  </Tr>
                )
              })}
              {!isLoading && filtered.length === 0 && (
                <Tr>
                  <Td colSpan={canManage ? 7 : 6} className="py-10 text-center">
                    <UserX className="mx-auto mb-2 h-6 w-6 text-muted" />
                    <div className="text-sm text-muted">{filtering ? 'No users match these filters.' : 'No users yet.'}</div>
                    {filtering && (
                      <Button variant="link" size="sm" className="mt-1" onClick={() => { setSearch(''); setRoleFilter('all'); setStatusFilter('all') }}>Clear filters</Button>
                    )}
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <UserFormDialog open={formOpen} onOpenChange={setFormOpen} user={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete user"
        description={
          <>
            Permanently delete <span className="font-semibold text-text">{deleting?.username}</span>?
            {deleting && (deleting.auth_source || 'local') !== 'local' && (
              <> The account will be re-created on their next successful {SOURCE_LABEL[deleting.auth_source!]} sign-in if account creation is on. Disable the user instead to block them.</>
            )}
          </>
        }
        confirmText="Delete" destructive loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
      <ResetPasswordDialog user={resetTarget} onClose={() => setResetTarget(null)} />
    </div>
  )
}

function ResetPasswordDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const reset = useMutation({
    mutationFn: async () => api.post(`/users/${user!.id}/reset-password`, { new_password: pw }),
    onSuccess: () => { toast.success('Password reset', `${user?.username} can sign in with the new password.`); close() },
    onError: (e: any) => toast.error('Reset failed', apiErrorMessage(e)),
  })
  function close() { setPw(''); setPw2(''); onClose() }
  const mismatch = pw2.length > 0 && pw !== pw2
  const ok = pw.length >= 6 && pw === pw2

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Reset password</DialogTitle>
          <DialogDescription>
            Set a new password for <span className="font-medium text-text">{user?.username}</span>. Their existing sessions stay signed in.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); if (ok) reset.mutate() }} className="space-y-3">
          <FormField label="New password" required hint="Minimum 6 characters">
            <PasswordInput value={pw} onChange={(e) => setPw(e.target.value)} autoFocus />
          </FormField>
          <FormField label="Confirm password" required error={mismatch ? 'Passwords do not match' : null}>
            <PasswordInput value={pw2} onChange={(e) => setPw2(e.target.value)} />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>Cancel</Button>
            <Button type="submit" disabled={!ok || reset.isPending}>
              {reset.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Reset password
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
