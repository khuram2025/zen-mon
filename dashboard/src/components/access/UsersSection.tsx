import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Download, Eye, KeyRound, Loader2, LockOpen, LogOut,
  MoreHorizontal, Pencil, Plus, Search, ShieldOff, Trash2, UserX, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import type { Role, User } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { StatusDot } from '@/components/ui/StatusDot'
import { Skeleton } from '@/components/ui/Skeleton'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/DropdownMenu'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryFailure } from '@/components/ui/QueryState'
import { UserFormDialog } from '@/components/forms/UserFormDialog'
import { toast } from '@/components/ui/Toast'
import { useAuth, useCan } from '@/stores/auth'
import { TagBadge } from '@/components/tags/TagBadge'
import { tagColor, tagColorMap, useTags } from '@/hooks/useTags'
import { ResetPasswordDialog, UserDetailDialog } from './UserDialogs'

const SOURCE_LABEL: Record<string, string> = { local: 'Local', ldap: 'LDAP / AD', radius: 'RADIUS', oidc: 'SSO (OIDC)' }
const PAGE_SIZE = 25
type SortKey = 'username' | 'role' | 'status' | 'last_login' | 'created'

function initials(u: User) {
  const src = (u.full_name || u.username).trim()
  const parts = src.split(/\s+/).filter(Boolean)
  return (parts.length >= 2 ? parts[0][0] + parts[1][0] : src.slice(0, 2)).toUpperCase()
}

/** Filters live in the URL so a filtered view survives refresh and can be
 *  shared or bookmarked (e.g. from a role's “Open in Users” link). */
function useUserFilters() {
  const [sp, setSp] = useSearchParams()
  const get = (k: string, d = '') => sp.get(k) ?? d
  const set = (patch: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === '' || v === 'all') next.delete(k)
      else next.set(k, v)
    }
    if (resetPage && !('page' in patch)) next.delete('page')
    setSp(next, { replace: true })
  }
  const filters = {
    q: get('q'), role: get('role', 'all'), status: get('status', 'all'), source: get('source', 'all'),
    scope: get('scope', 'all'), signin: get('signin', 'all'), mfa: get('mfa', 'all'),
    sort: (get('sort', 'username') as SortKey), order: get('order', 'asc') as 'asc' | 'desc',
    page: Math.max(1, Number(get('page', '1')) || 1),
  }
  const active = filters.q !== '' || filters.role !== 'all' || filters.status !== 'all' || filters.source !== 'all'
    || filters.scope !== 'all' || filters.signin !== 'all' || filters.mfa !== 'all'
  const clear = () => {
    const next = new URLSearchParams(sp)
    for (const k of ['q', 'role', 'status', 'source', 'scope', 'signin', 'mfa', 'page']) next.delete(k)
    setSp(next, { replace: true })
  }
  return { filters, set, active, clear }
}

export function UsersSection() {
  const qc = useQueryClient()
  const can = useCan()
  const me = useAuth((s) => s.user)
  const canManage = can('users.manage')
  const { filters, set, active: filtering, clear } = useUserFilters()
  const [qInput, setQInput] = useState(filters.q)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [deleting, setDeleting] = useState<User | null>(null)
  const [resetTarget, setResetTarget] = useState<User | null>(null)
  const [detail, setDetail] = useState<User | null>(null)
  const [mfaReset, setMfaReset] = useState<User | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<User | null>(null)

  // Debounce typing into the URL.
  useEffect(() => { setQInput(filters.q) }, [filters.q])
  useEffect(() => {
    if (qInput === filters.q) return
    const t = window.setTimeout(() => set({ q: qInput.trim() }), 300)
    return () => window.clearTimeout(t)
  }, [qInput]) // eslint-disable-line react-hooks/exhaustive-deps

  const params = useMemo(() => {
    const p: Record<string, string | number | boolean> = {
      sort: filters.sort, order: filters.order, limit: PAGE_SIZE, offset: (filters.page - 1) * PAGE_SIZE,
    }
    if (filters.q) p.q = filters.q
    if (filters.role !== 'all') p.role = filters.role
    if (filters.status !== 'all') p.status = filters.status
    if (filters.source !== 'all') p.source = filters.source
    if (filters.scope !== 'all') p.scoped = filters.scope === 'scoped'
    if (filters.signin !== 'all') p.never_signed_in = filters.signin === 'never'
    if (filters.mfa !== 'all') p.mfa = filters.mfa === 'on'
    return p
  }, [filters])

  const usersQ = useQuery<{ rows: User[]; total: number }>({
    queryKey: ['users', 'list', params],
    queryFn: async () => {
      const r = await api.get('/users', { params })
      return { rows: r.data as User[], total: Number(r.headers['x-total-count'] ?? (r.data as User[]).length) }
    },
    placeholderData: keepPreviousData,
  })
  const rolesQ = useQuery<Role[]>({ queryKey: ['roles'], queryFn: async () => (await api.get('/roles')).data })
  const { data: tagDefs } = useTags()
  const tagColors = useMemo(() => tagColorMap(tagDefs), [tagDefs])

  const roleLabel = useMemo(() => Object.fromEntries((rolesQ.data || []).map((r) => [r.name, r.display_name])), [rolesQ.data])
  const adminRoles = useMemo(() => new Set((rolesQ.data || []).filter((r) => r.permissions.includes('system.admin')).map((r) => r.name)), [rolesQ.data])

  const rows = usersQ.data?.rows || []
  const total = usersQ.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const colSpan = canManage ? 8 : 7

  const invalidate = () => { qc.invalidateQueries({ queryKey: ['users'] }); qc.invalidateQueries({ queryKey: ['roles'] }) }
  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/${id}`),
    onSuccess: () => { toast.success('User deleted'); invalidate(); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const unlock = useMutation({
    mutationFn: async (id: string) => api.post(`/users/${id}/unlock`),
    onSuccess: () => { toast.success('Account unlocked'); invalidate() },
    onError: (e: any) => toast.error('Unlock failed', apiErrorMessage(e)),
  })
  const revoke = useMutation({
    mutationFn: async (id: string) => api.post(`/users/${id}/revoke-sessions`),
    onSuccess: () => { toast.success('Signed out everywhere'); setRevokeTarget(null); invalidate() },
    onError: (e: any) => toast.error('Could not revoke sessions', apiErrorMessage(e)),
  })
  const resetMfa = useMutation({
    mutationFn: async (id: string) => api.post(`/users/${id}/mfa/reset`),
    onSuccess: () => { toast.success('Multi-factor authentication removed'); setMfaReset(null); invalidate() },
    onError: (e: any) => toast.error('Could not reset MFA', apiErrorMessage(e)),
  })
  const exporting = useMutation({
    mutationFn: async () => {
      const r = await api.get('/users/access-review', { responseType: 'blob' })
      const url = URL.createObjectURL(r.data as Blob)
      const a = document.createElement('a')
      a.href = url; a.download = `zenplus-access-review-${new Date().toISOString().slice(0, 10)}.csv`; a.click()
      URL.revokeObjectURL(url)
    },
    onSuccess: () => toast.success('Access review exported'),
    onError: (e: any) => toast.error('Export failed', apiErrorMessage(e)),
  })

  function sortBy(key: SortKey) {
    if (filters.sort === key) set({ order: filters.order === 'asc' ? 'desc' : 'asc' }, false)
    else set({ sort: key, order: 'asc' }, false)
  }
  function SortTh({ k, children, className }: { k: SortKey; children: React.ReactNode; className?: string }) {
    const on = filters.sort === k
    const Icon = !on ? ArrowUpDown : filters.order === 'asc' ? ArrowUp : ArrowDown
    return (
      <Th className={className} aria-sort={on ? (filters.order === 'asc' ? 'ascending' : 'descending') : 'none'}>
        <button type="button" className="inline-flex items-center gap-1 hover:text-text" onClick={() => sortBy(k)}>
          {children}<Icon className={cn('h-3 w-3', !on && 'opacity-40')} aria-hidden />
        </button>
      </Th>
    )
  }

  const filterSelect = (key: 'role' | 'status' | 'source' | 'scope' | 'signin' | 'mfa', label: string, width: string, options: [string, string][]) => (
    <Select value={filters[key]} onValueChange={(v) => set({ [key]: v })}>
      <SelectTrigger className={width} aria-label={label}><SelectValue /></SelectTrigger>
      <SelectContent>{options.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
    </Select>
  )

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-1 flex-wrap items-center gap-2">
          <div className="relative w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
            <Input className="pl-8" aria-label="Search users" placeholder="Search name, username, email…" value={qInput} onChange={(e) => setQInput(e.target.value)} />
          </div>
          {filterSelect('role', 'Filter by role', 'w-40', [['all', 'All roles'], ...(rolesQ.data || []).map((r): [string, string] => [r.name, r.display_name])])}
          {filterSelect('status', 'Filter by status', 'w-32', [['all', 'Any status'], ['active', 'Active'], ['disabled', 'Disabled'], ['locked', 'Locked']])}
          {filterSelect('source', 'Filter by sign-in source', 'w-32', [['all', 'Any source'], ['local', 'Local'], ['ldap', 'LDAP / AD'], ['radius', 'RADIUS'], ['oidc', 'SSO (OIDC)']])}
          {filterSelect('scope', 'Filter by visibility scope', 'w-36', [['all', 'Any scope'], ['scoped', 'Tag-scoped'], ['unscoped', 'All resources']])}
          {filterSelect('signin', 'Filter by sign-in history', 'w-36', [['all', 'Any sign-in'], ['never', 'Never signed in'], ['ever', 'Has signed in']])}
          {filterSelect('mfa', 'Filter by multi-factor authentication', 'w-28', [['all', 'Any MFA'], ['on', 'MFA on'], ['off', 'MFA off']])}
          {filtering && <Button variant="ghost" size="sm" onClick={() => { clear(); setQInput('') }}><X className="h-3.5 w-3.5" /> Clear filters</Button>}
        </div>
        <div className="flex gap-2">
          {canManage && (
            <Button variant="outline" onClick={() => exporting.mutate()} disabled={exporting.isPending} title="CSV of every account, role, privileged permissions, scope, MFA and last sign-in, for periodic access reviews">
              {exporting.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />} Access review
            </Button>
          )}
          {canManage && <Button onClick={() => { setEditing(null); setFormOpen(true) }}><Plus className="h-4 w-4" /> New user</Button>}
        </div>
      </div>

      <p className="text-xs text-muted" aria-live="polite">
        {usersQ.data ? <>{total} {total === 1 ? 'user' : 'users'}{filtering ? ' match these filters' : ''}</> : usersQ.isError ? 'User count unavailable' : 'Loading users…'}
        {usersQ.isFetching && usersQ.data && <Loader2 className="ml-2 inline h-3 w-3 animate-spin" aria-label="Refreshing" />}
        {rolesQ.isError && <span className="ml-2 text-warning">Role names unavailable</span>}
      </p>

      {usersQ.isError && !usersQ.data && <QueryFailure error={usersQ.error} what="users" onRetry={() => void usersQ.refetch()} retrying={usersQ.isFetching} />}
      {usersQ.isError && usersQ.data && <QueryFailure compact error={usersQ.error} what="users" onRetry={() => void usersQ.refetch()} retrying={usersQ.isFetching} />}

      {!(usersQ.isError && !usersQ.data) && (
        <Card>
          <CardContent className="p-0">
            <Table>
              <caption className="sr-only">User accounts</caption>
              <THead className="bg-surface2/50">
                <Tr>
                  <SortTh k="username">User</SortTh>
                  <SortTh k="role">Role</SortTh>
                  <Th>Visibility</Th>
                  <Th>Source</Th>
                  <SortTh k="status">Status</SortTh>
                  <Th>MFA</Th>
                  <SortTh k="last_login">Last sign-in</SortTh>
                  {canManage && <Th className="w-28 text-right">Actions</Th>}
                </Tr>
              </THead>
              <TBody>
                {usersQ.isLoading && Array.from({ length: 4 }).map((_, i) => (
                  <Tr key={i}><Td colSpan={colSpan} className="py-2.5"><Skeleton className="h-8 w-full" /></Td></Tr>
                ))}
                {rows.map((u) => {
                  const isMe = me?.id === u.id
                  const external = (u.auth_source || 'local') !== 'local'
                  const active = u.is_active !== false
                  const isAdmin = adminRoles.has(u.role)
                  return (
                    <Tr key={u.id} className={cn('cursor-pointer', !active && 'opacity-70')} onClick={() => setDetail(u)}>
                      <Td>
                        <div className="flex items-center gap-3">
                          <div className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold', isAdmin ? 'bg-primary/15 text-primary' : 'bg-surface2 text-muted')} aria-hidden>
                            {initials(u)}
                          </div>
                          <div className="min-w-0">
                            <button type="button" className="flex items-center gap-1.5 text-left text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" aria-label={`Account details for ${u.username}`} onClick={(e) => { e.stopPropagation(); setDetail(u) }}>
                              <span className="truncate">{u.full_name || u.username}</span>
                              {isMe && <Badge variant="outline" className="px-1.5 py-0 text-[10px]">You</Badge>}
                            </button>
                            <div className="truncate text-[11px] text-muted">{u.full_name ? <>{u.username} · </> : null}{u.email}</div>
                          </div>
                        </div>
                      </Td>
                      <Td><Badge variant={isAdmin ? 'success' : 'info'}>{roleLabel[u.role] || u.role}</Badge></Td>
                      <Td>
                        {isAdmin ? <span className="text-xs text-muted" title="Full administration bypasses visibility scope">All resources</span>
                          : (u.scope_tags || []).length === 0 ? <span className="text-xs text-muted">All resources</span>
                          : (
                            <div className="flex max-w-[220px] flex-wrap gap-1">
                              {(u.scope_tags || []).slice(0, 3).map((t) => <TagBadge key={t} name={t} color={tagColor(t, tagColors)} />)}
                              {(u.scope_tags || []).length > 3 && <span className="text-[10px] text-muted">+{(u.scope_tags || []).length - 3}</span>}
                            </div>
                          )}
                      </Td>
                      <Td className="text-xs">{SOURCE_LABEL[u.auth_source || 'local'] || u.auth_source}</Td>
                      <Td>
                        <span className="inline-flex items-center gap-2 text-xs">
                          <StatusDot status={u.locked ? 'down' : active ? 'up' : 'idle'} />
                          {u.locked ? 'Locked' : active ? 'Active' : 'Disabled'}
                          {u.must_change_password && <Badge variant="outline" className="px-1 py-0 text-[9px]">pw change due</Badge>}
                        </span>
                      </Td>
                      <Td className="text-xs">{external ? <span className="text-muted">via directory</span> : u.mfa_enabled ? <Badge variant="success">On</Badge> : <span className="text-muted">Off</span>}</Td>
                      <Td className="text-xs text-muted">
                        <time dateTime={u.last_login || undefined} title={u.last_login ? new Date(u.last_login).toLocaleString(undefined, { timeZoneName: 'short' }) : 'Has never signed in'}>
                          {relativeTime(u.last_login)}
                        </time>
                      </Td>
                      {canManage && (
                        <Td onClick={(e) => e.stopPropagation()}>
                          <div className="flex justify-end gap-0.5">
                            <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" aria-label={`Edit ${u.username}`} onClick={() => { setEditing(u); setFormOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <Button variant="ghost" size="icon" className="h-7 w-7" aria-label={`More actions for ${u.username}`}><MoreHorizontal className="h-3.5 w-3.5" /></Button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent>
                                <DropdownMenuItem onSelect={() => setDetail(u)}><Eye /> Details &amp; effective access</DropdownMenuItem>
                                <DropdownMenuItem disabled={external || isMe} onSelect={() => setResetTarget(u)}><KeyRound /> Reset password…</DropdownMenuItem>
                                {u.locked && <DropdownMenuItem onSelect={() => unlock.mutate(u.id)}><LockOpen /> Unlock account</DropdownMenuItem>}
                                <DropdownMenuItem onSelect={() => setRevokeTarget(u)}><LogOut /> Sign out everywhere…</DropdownMenuItem>
                                {u.mfa_enabled && <DropdownMenuItem onSelect={() => setMfaReset(u)}><ShieldOff /> Remove MFA…</DropdownMenuItem>}
                                <DropdownMenuSeparator />
                                <DropdownMenuItem destructive disabled={isMe} onSelect={() => setDeleting(u)}><Trash2 /> Delete…</DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </div>
                        </Td>
                      )}
                    </Tr>
                  )
                })}
                {usersQ.data && rows.length === 0 && (
                  <Tr>
                    <Td colSpan={colSpan} className="py-10 text-center">
                      <UserX className="mx-auto mb-2 h-6 w-6 text-muted" aria-hidden />
                      <div className="text-sm text-muted">{filtering ? 'No users match these filters.' : 'No users yet.'}</div>
                      {filtering && <Button variant="link" size="sm" className="mt-1" onClick={() => { clear(); setQInput('') }}>Clear filters</Button>}
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {pages > 1 && (
        <nav className="flex items-center justify-between text-xs text-muted" aria-label="User list pages">
          <span>Page {filters.page} of {pages}</span>
          <div className="flex gap-1">
            <Button variant="outline" size="sm" disabled={filters.page <= 1} onClick={() => set({ page: String(filters.page - 1) }, false)}><ChevronLeft className="h-3.5 w-3.5" /> Previous</Button>
            <Button variant="outline" size="sm" disabled={filters.page >= pages} onClick={() => set({ page: String(filters.page + 1) }, false)}>Next <ChevronRight className="h-3.5 w-3.5" /></Button>
          </div>
        </nav>
      )}

      <UserFormDialog open={formOpen} onOpenChange={setFormOpen} user={editing} />
      <UserDetailDialog user={detail} onClose={() => setDetail(null)} canManage={canManage} />
      <ResetPasswordDialog user={resetTarget} onClose={() => setResetTarget(null)} />

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
      <ConfirmDialog
        open={!!revokeTarget}
        onOpenChange={(o) => !o && setRevokeTarget(null)}
        title="Sign out everywhere"
        description={<>Sign <span className="font-semibold text-text">{revokeTarget?.username}</span> out of every session? They stay enabled and can sign in again.</>}
        confirmText="Sign out" loading={revoke.isPending}
        onConfirm={() => { if (revokeTarget) revoke.mutate(revokeTarget.id) }}
      />
      <ConfirmDialog
        open={!!mfaReset}
        onOpenChange={(o) => !o && setMfaReset(null)}
        title="Remove multi-factor authentication"
        description={<>Remove the authenticator for <span className="font-semibold text-text">{mfaReset?.username}</span> (for example a lost phone) and sign them out everywhere? If policy requires MFA they must enrol again at next sign-in.</>}
        confirmText="Remove MFA" destructive loading={resetMfa.isPending}
        onConfirm={() => { if (mfaReset) resetMfa.mutate(mfaReset.id) }}
      />
    </div>
  )
}
