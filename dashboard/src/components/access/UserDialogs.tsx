import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Dices, Loader2, LogOut, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, copyText, relativeTime } from '@/lib/utils'
import type { User } from '@/types'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { QueryBoundary } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { NewPasswordFields, usePasswordCheck } from '@/components/security/PasswordFields'
import { useCan } from '@/stores/auth'

/** Strong random password: ~130 bits from an unambiguous alphabet. */
function generatePassword(): string {
  const alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
  const bytes = new Uint32Array(22)
  crypto.getRandomValues(bytes)
  const raw = Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('')
  return `${raw.slice(0, 7)}-${raw.slice(7, 14)}-${raw.slice(14)}`
}

export function ResetPasswordDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [mustChange, setMustChange] = useState(true)
  const [generated, setGenerated] = useState(false)
  const check = usePasswordCheck(pw, pw2, { username: user?.username, email: user?.email })

  function close() { setPw(''); setPw2(''); setGenerated(false); setMustChange(true); onClose() }

  const reset = useMutation({
    mutationFn: async () => (await api.post(`/users/${user!.id}/reset-password`, { new_password: pw, must_change_password: mustChange })).data,
    onSuccess: (d) => {
      toast.success('Password reset', `${user?.username} was signed out of ${d.sessions_revoked ?? 'all'} session(s) and must sign in with the new password.`)
      qc.invalidateQueries({ queryKey: ['users'] })
      close()
    },
    onError: (e: any) => toast.error('Reset failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && !reset.isPending && close()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Reset password</DialogTitle>
          <DialogDescription>
            Set a new password for <span className="font-medium text-text">{user?.username}</span>. <strong>Every existing session of this account is signed out</strong>, and they must sign in again with the new password. If a mail gateway is configured they receive a notice (without the password); give them the password through a secure channel.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); if (check.ok) reset.mutate() }} className="space-y-3">
          <NewPasswordFields password={pw} onPassword={(v) => { setPw(v); setGenerated(false) }} confirm={pw2} onConfirm={setPw2} ctx={{ username: user?.username, email: user?.email }} autoFocus />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" size="sm" variant="outline" onClick={() => { const g = generatePassword(); setPw(g); setPw2(g); setGenerated(true) }}><Dices className="h-3.5 w-3.5" /> Generate</Button>
            {generated && (
              <>
                <code className="select-all break-all rounded bg-surface2 px-2 py-1 font-mono text-xs">{pw}</code>
                <Button type="button" size="icon" variant="ghost" className="h-7 w-7" aria-label="Copy password" onClick={async () => { (await copyText(pw)) ? toast.success('Copied') : toast.error('Copy failed') }}><Copy className="h-3.5 w-3.5" /></Button>
              </>
            )}
          </div>
          <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2">
            <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-primary" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
            <span className="text-xs"><span className="font-medium">Require a new password at next sign-in</span><br /><span className="text-muted">Recommended: you chose this password.</span></span>
          </label>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close} disabled={reset.isPending}>Cancel</Button>
            <Button type="submit" disabled={!check.ok || reset.isPending}>
              {reset.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Reset password
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

type EffectiveAccess = {
  user: User
  role: { name: string; display_name: string; exists: boolean; full_administration: boolean }
  permissions: { module: string; label: string; granted_count: number; total: number; permissions: { id: string; label: string; granted: boolean; risk: string }[] }[]
  permission_count: number
  privileged_permissions: string[]
  scope: { state: 'all' | 'scoped'; tags: string[]; ignored_because_full_administration: boolean; note: string }
  sessions: number
  history: { at: string; action: string; actor: string | null; details: Record<string, unknown> }[]
}
type SessionRow = { id: string; created_at: string; last_seen_at: string; ip_address: string | null; user_agent: string | null; auth_method: string; mfa_verified: boolean }

const when = (iso: string | null | undefined) => iso ? new Date(iso).toLocaleString(undefined, { timeZoneName: 'short' }) : 'Never'

/** Account detail: what the account can actually do (effective access), where
 *  it is signed in (with per-session revoke), and the recent changes to it. */
export function UserDetailDialog({ user, onClose, canManage }: { user: User | null; onClose: () => void; canManage: boolean }) {
  const qc = useQueryClient()
  const can = useCan()
  const access = useQuery<EffectiveAccess>({
    queryKey: ['users', user?.id, 'effective-access'],
    queryFn: async () => (await api.get(`/users/${user!.id}/effective-access`)).data,
    enabled: !!user,
  })
  const sessions = useQuery<{ data: SessionRow[] }>({
    queryKey: ['users', user?.id, 'sessions'],
    queryFn: async () => (await api.get(`/users/${user!.id}/sessions`)).data,
    enabled: !!user && canManage && can('users.manage'),
  })
  const revoke = useMutation({
    mutationFn: async (sid: string) => api.delete(`/users/${user!.id}/sessions/${sid}`),
    onSuccess: () => { toast.success('Session revoked'); qc.invalidateQueries({ queryKey: ['users', user?.id, 'sessions'] }) },
    onError: (e: any) => toast.error('Could not revoke', apiErrorMessage(e)),
  })
  const revokeAll = useMutation({
    mutationFn: async () => api.post(`/users/${user!.id}/revoke-sessions`),
    onSuccess: () => { toast.success('Signed out everywhere'); qc.invalidateQueries({ queryKey: ['users', user?.id, 'sessions'] }); qc.invalidateQueries({ queryKey: ['users', user?.id, 'effective-access'] }) },
    onError: (e: any) => toast.error('Could not revoke', apiErrorMessage(e)),
  })

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{user?.full_name || user?.username}</DialogTitle>
          <DialogDescription>{user?.username} · {user?.email} · created {when(user?.created_at)}</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="access">
          <TabsList className="bg-surface2/50 p-1">
            <TabsTrigger value="access" className="text-xs">Effective access</TabsTrigger>
            {canManage && <TabsTrigger value="sessions" className="text-xs">Sessions</TabsTrigger>}
            <TabsTrigger value="history" className="text-xs">Recent changes</TabsTrigger>
          </TabsList>

          <TabsContent value="access" className="mt-3 space-y-3">
            <QueryBoundary query={access} what="this account's access" skeleton={<Skeleton className="h-32 w-full" />}>
              {(a) => (
                <>
                  <dl className="grid grid-cols-[130px_1fr] gap-y-1.5 text-sm">
                    <dt className="text-xs text-muted">Role</dt>
                    <dd>{a.role.display_name} {a.role.full_administration && <Badge variant="warning"><ShieldAlert className="h-3 w-3" /> Full administration</Badge>} {!a.role.exists && <Badge variant="danger">Role missing — no access</Badge>}</dd>
                    <dt className="text-xs text-muted">Sign-in</dt>
                    <dd>{(a.user.auth_source || 'local').toUpperCase()} {a.user.mfa_enabled ? '· MFA on' : '· no MFA'} {a.user.locked && <Badge variant="danger">Locked</Badge>} {a.user.must_change_password && <Badge variant="outline">Password change pending</Badge>}</dd>
                    <dt className="text-xs text-muted">Visibility</dt>
                    <dd className="text-xs">{a.scope.state === 'all' ? 'All resources the role permits' : `Only resources tagged: ${a.scope.tags.join(', ')}`}<div className="text-muted">{a.scope.note}</div></dd>
                    <dt className="text-xs text-muted">Last sign-in</dt>
                    <dd className="text-xs">{when(a.user.last_login)} <span className="text-muted">({relativeTime(a.user.last_login)})</span></dd>
                    <dt className="text-xs text-muted">Open sessions</dt>
                    <dd className="text-xs">{a.sessions}</dd>
                  </dl>
                  {a.privileged_permissions.length > 0 && !a.role.full_administration && (
                    <p role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs"><ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" /> Privileged: {a.privileged_permissions.join(', ')}</p>
                  )}
                  <div className="overflow-hidden rounded-md border border-border">
                    <table className="w-full text-xs">
                      <caption className="sr-only">Permissions this account holds, by module</caption>
                      <tbody>
                        {a.permissions.map((m) => (
                          <tr key={m.module} className="border-b border-border last:border-0">
                            <th scope="row" className="w-44 px-3 py-1.5 text-left font-medium">{m.label} <span className="font-normal text-muted">({m.granted_count}/{m.total})</span></th>
                            <td className="px-3 py-1.5">
                              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                                {m.permissions.map((p) => (
                                  <span key={p.id} className={p.granted ? 'text-text' : 'text-muted/50 line-through'}>
                                    <span className="sr-only">{p.granted ? 'Has ' : 'Lacks '}</span>{p.label}
                                  </span>
                                ))}
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </QueryBoundary>
          </TabsContent>

          {canManage && (
            <TabsContent value="sessions" className="mt-3 space-y-2">
              <QueryBoundary query={sessions} what="sessions" skeleton={<Skeleton className="h-24 w-full" />}>
                {(s) => s.data.length === 0 ? (
                  <p className="py-6 text-center text-sm text-muted">No open sessions. (Sessions started before this release are not listed.)</p>
                ) : (
                  <>
                    <ul className="divide-y divide-border rounded-md border border-border">
                      {s.data.map((row) => (
                        <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs">
                          <div className="min-w-0">
                            <div className="font-medium">{row.ip_address || 'unknown address'} · {row.auth_method}{row.mfa_verified ? ' · MFA' : ''}</div>
                            <div className="truncate text-muted" title={row.user_agent || ''}>{row.user_agent || 'unknown client'}</div>
                            <div className="text-muted">Started {when(row.created_at)} · last seen {relativeTime(row.last_seen_at)}</div>
                          </div>
                          <Button size="sm" variant="outline" onClick={() => revoke.mutate(row.id)} disabled={revoke.isPending}><LogOut className="h-3.5 w-3.5" /> Revoke</Button>
                        </li>
                      ))}
                    </ul>
                    <div className="flex justify-end"><Button size="sm" variant="destructive" onClick={() => revokeAll.mutate()} disabled={revokeAll.isPending}>Sign out everywhere</Button></div>
                  </>
                )}
              </QueryBoundary>
            </TabsContent>
          )}

          <TabsContent value="history" className="mt-3">
            <QueryBoundary query={access} what="account history" skeleton={<Skeleton className="h-24 w-full" />}>
              {(a) => a.history.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted">No recorded changes.</p>
              ) : (
                <ol className="space-y-1.5">
                  {a.history.map((h, i) => (
                    <li key={i} className="rounded-md border border-border px-3 py-1.5 text-xs">
                      <div className="flex flex-wrap justify-between gap-1"><span className="font-medium">{h.action}</span><span className="text-muted">{h.actor || 'system'} · {when(h.at)}</span></div>
                      {h.details && Object.keys(h.details).length > 0 && <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-2xs text-muted">{JSON.stringify(h.details, null, 1)}</pre>}
                    </li>
                  ))}
                </ol>
              )}
            </QueryBoundary>
          </TabsContent>
        </Tabs>
        <DialogFooter><Button variant="outline" onClick={onClose}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
void Input
