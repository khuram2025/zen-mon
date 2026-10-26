import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Info, Loader2, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { describeFailure } from '@/lib/access'
import type { Role, User } from '@/types'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { QueryFailure } from '@/components/ui/QueryState'
import { TagPicker } from '@/components/tags/TagPicker'
import { ScopeChips } from '@/components/access/MenuPreview'
import { NewPasswordFields, usePasswordCheck } from '@/components/security/PasswordFields'
import { useAuth } from '@/stores/auth'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'

const SOURCE_LABEL: Record<string, string> = { ldap: 'LDAP / Active Directory', radius: 'RADIUS', oidc: 'OpenID Connect single sign-on' }
const EMAIL_RE = /^[^@\s<>()[\],;:\\"]+@[^@\s<>()[\],;:\\"]+\.[^@\s<>()[\],;:\\"]{2,}$/

type Assignable = { name: string; display_name: string; description: string; permission_count: number; privileged: boolean }

/** Create or edit a user.
 *
 *  - only roles the signed-in account may assign are offered (the server
 *    enforces the same ceiling);
 *  - edit sends only what changed, so a name edit never revokes sessions;
 *  - the visibility scope is an explicit choice (All resources / Selected tags)
 *    and is shown as locked for administrators, who bypass it;
 *  - password rules come from the API, so the form never promises less. */
export function UserFormDialog({ open, onOpenChange, user }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  user?: User | null
}) {
  const isEdit = !!user?.id
  const source = user?.auth_source || 'local'
  const isExternal = isEdit && source !== 'local'
  const me = useAuth((s) => s.user)
  const isSelf = isEdit && me?.id === user?.id
  const qc = useQueryClient()

  const assignable = useQuery<Assignable[]>({
    queryKey: ['roles', 'assignable'],
    queryFn: async () => (await api.get('/roles/assignable')).data,
    enabled: open,
    staleTime: 0,
  })
  const allRoles = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
    enabled: open,
  })

  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [role, setRole] = useState('viewer')
  const [isActive, setIsActive] = useState(true)
  const [scopeMode, setScopeMode] = useState<'all' | 'tags'>('all')
  const [scopeTags, setScopeTags] = useState<string[]>([])
  const [mustChange, setMustChange] = useState(true)
  const [errors, setErrors] = useState<{ username?: string; email?: string; general?: string; password?: string }>({})
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    setUsername(user?.username || '')
    setEmail(user?.email || '')
    setFullName(user?.full_name || '')
    setPassword(''); setPassword2('')
    setRole(user?.role || 'viewer')
    setIsActive(user?.is_active ?? true)
    setScopeTags(user?.scope_tags || [])
    setScopeMode((user?.scope_tags || []).length > 0 ? 'tags' : 'all')
    setMustChange(true)
    setErrors({}); setSubmitted(false)
  }, [open, user])

  const roleMeta = useMemo(() => {
    const m: Record<string, Role> = {}
    for (const r of allRoles.data || []) m[r.name] = r
    return m
  }, [allRoles.data])
  const roleOptions = assignable.data || []
  const currentRoleInList = roleOptions.some((r) => r.name === role)
  const roleIsAdmin = !!roleMeta[role]?.permissions.includes('system.admin')
  const roleScope = roleMeta[role]?.scope_tags || []
  const scopeOptions: [('all' | 'tags'), string][] = roleScope.length
    ? [['all', 'Role scope only'], ['tags', 'Add tags for this user']]
    : [['all', 'All resources'], ['tags', 'Selected tags']]
  const roleAboveYou = isEdit && !!user && !roleOptions.some((r) => r.name === user.role) && assignable.isSuccess

  const pwCheck = usePasswordCheck(password, password2, { username: username.trim() || user?.username, email })
  const emailError = !email.trim() ? 'Email is required' : !EMAIL_RE.test(email.trim()) ? 'Enter a valid email address' : undefined
  const usernameError = isEdit ? undefined : username.trim().length < 3 ? 'Username must be at least 3 characters' : username.trim().length > 100 ? 'Username can be at most 100 characters' : undefined
  const scopeError = scopeMode === 'tags' && scopeTags.length === 0 ? 'Choose at least one tag, or switch to “All resources”' : undefined
  const valid = !emailError && !usernameError && !scopeError && (isEdit || pwCheck.ok)

  const effectiveScope = scopeMode === 'all' ? [] : scopeTags
  const dirty = isEdit
    ? email.trim().toLowerCase() !== (user?.email || '').toLowerCase()
      || (fullName.trim() || null) !== (user?.full_name || null)
      || (!isExternal && !isSelf && role !== user?.role)
      || isActive !== (user?.is_active ?? true)
      || JSON.stringify([...effectiveScope].sort()) !== JSON.stringify([...(user?.scope_tags || [])].sort())
    : !!(username || email || fullName || password)

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        // Only what changed: unchanged access fields would needlessly revoke sessions.
        const payload: Record<string, unknown> = {}
        if (email.trim().toLowerCase() !== (user!.email || '').toLowerCase()) payload.email = email.trim()
        if ((fullName.trim() || null) !== (user!.full_name || null)) payload.full_name = fullName.trim() || null
        if (!isExternal && !isSelf && role !== user!.role) payload.role = role
        if (isActive !== (user!.is_active ?? true)) payload.is_active = isActive
        if (JSON.stringify([...effectiveScope].sort()) !== JSON.stringify([...(user!.scope_tags || [])].sort())) payload.scope_tags = effectiveScope
        if (Object.keys(payload).length === 0) return { unchanged: true }
        return (await api.put(`/users/${user!.id}`, payload)).data
      }
      return (await api.post('/users', {
        username: username.trim(), email: email.trim(), full_name: fullName.trim() || null,
        password, role, scope_tags: effectiveScope, must_change_password: mustChange,
      })).data
    },
    onSuccess: (r: any) => {
      if (r?.unchanged) toast.info('Nothing to change')
      else toast.success(isEdit ? 'User updated' : 'User created')
      qc.invalidateQueries({ queryKey: ['users'] })
      qc.invalidateQueries({ queryKey: ['roles'] })
      onOpenChange(false)
    },
    onError: (e: any) => {
      const f = describeFailure(e)
      if (f.status === 409 && /username/i.test(f.message)) setErrors({ username: f.message })
      else if (f.status === 409 && /email/i.test(f.message)) setErrors({ email: f.message })
      else if (f.status === 422 && /password/i.test(f.message)) setErrors({ password: f.message })
      else setErrors({ general: f.message })
    },
  })

  const guard = useDirtyGuard(dirty && !save.isSuccess, save.isPending, onOpenChange)

  function submit(e: FormEvent) {
    e.preventDefault()
    setSubmitted(true)
    setErrors({})
    if (!valid) return
    save.mutate()
  }

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{isEdit ? `Edit user: ${user?.username}` : 'New local user'}</DialogTitle>
            <DialogDescription>
              {isExternal
                ? `This account signs in through ${SOURCE_LABEL[source]}. Its password and role are managed by the directory and re-applied on every sign-in.`
                : isEdit
                  ? 'Local account with a password stored on this appliance.'
                  : 'Creates a local account. Directory users are created automatically on their first sign-in.'}
            </DialogDescription>
          </DialogHeader>
          <form noValidate onSubmit={submit} className="space-y-4">
            {errors.general && <div role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">{errors.general}</div>}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="Username" required hint={isEdit ? 'Usernames cannot be changed.' : '3–100 characters. Used to sign in; unique ignoring case.'} error={(submitted || username) ? (errors.username || usernameError) : errors.username}>
                <Input value={username} onChange={(e) => { setUsername(e.target.value); setErrors({}) }} disabled={isEdit} placeholder="jsmith" autoComplete="off" autoFocus={!isEdit} aria-invalid={!!(errors.username || usernameError)} />
              </FormField>
              <FormField label="Full name">
                <Input value={fullName} maxLength={255} onChange={(e) => setFullName(e.target.value)} placeholder="John Smith" />
              </FormField>
            </div>
            <FormField label="Email" required error={(submitted || email) ? (errors.email || emailError) : errors.email}>
              <Input type="email" value={email} onChange={(e) => { setEmail(e.target.value); setErrors({}) }} placeholder="user@example.com" autoComplete="off" aria-invalid={!!(errors.email || emailError)} />
            </FormField>

            {!isEdit && (
              <>
                <NewPasswordFields password={password} onPassword={(v) => { setPassword(v); setErrors({}) }} confirm={password2} onConfirm={setPassword2} ctx={{ username, email }} label="Initial password" />
                {errors.password && <p role="alert" className="-mt-2 text-xs text-danger">{errors.password}</p>}
                <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2">
                  <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-primary" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
                  <span className="text-xs"><span className="font-medium">Require a new password at first sign-in</span><br /><span className="text-muted">Recommended: you chose this password, so only the user should keep using it.</span></span>
                </label>
              </>
            )}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField
                label="Role"
                hint={
                  isExternal ? 'Set by the directory role mapping; change the mapping instead.'
                  : isSelf ? 'You cannot change your own role.'
                  : roleAboveYou ? 'This account has more access than you do, so only a higher administrator can change it.'
                  : roleIsAdmin ? 'Full administration: sees everything and bypasses visibility scope.'
                  : roleMeta[role]?.description || undefined
                }
              >
                {/* Radix reports '' while the options are still loading; never store it as the role. */}
                <Select value={role} onValueChange={(v) => { if (v) setRole(v) }} disabled={isExternal || isSelf || roleAboveYou || assignable.isLoading}>
                  <SelectTrigger aria-label="Role"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {(currentRoleInList || !isEdit || !role ? roleOptions : [{ name: role, display_name: roleMeta[role]?.display_name || role }, ...roleOptions]).map((r) => (
                      <SelectItem key={r.name} value={r.name}>{r.display_name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              {isEdit && (
                <FormField label="Status" hint={isSelf ? 'You cannot disable your own account.' : isActive ? undefined : 'A disabled account cannot sign in and is signed out everywhere.'}>
                  <label className="flex h-9 cursor-pointer items-center justify-between rounded-md border border-border px-3">
                    <span className="text-sm">{isActive ? 'Active' : 'Disabled'}</span>
                    <Switch checked={isActive} onCheckedChange={setIsActive} disabled={isSelf} aria-label="Account active" />
                  </label>
                </FormField>
              )}
            </div>
            {assignable.isError && <QueryFailure compact error={assignable.error} what="the roles you may assign" onRetry={() => void assignable.refetch()} />}
            {roleOptions.some((r) => r.name === role && r.privileged) && !roleIsAdmin && (
              <p role="note" className="flex items-start gap-2 text-xs text-warning"><ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> This role can manage users or roles. Assign it deliberately.</p>
            )}

            <fieldset className="space-y-2">
              <legend className="mb-1 text-sm font-medium">Visibility scope</legend>
              {roleIsAdmin ? (
                <div className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
                  <span><span className="font-medium">All resources — full administration bypasses scope.</span>
                    {scopeTags.length > 0 && <> Stored tags ({scopeTags.join(', ')}) are kept and apply again if the role is later changed to a non-administrator role.</>}</span>
                </div>
              ) : (
                <>
                  {roleScope.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
                      <Info className="h-3.5 w-3.5 shrink-0 text-muted" />
                      <span className="font-medium">The {roleMeta[role]?.display_name} role limits its members to</span>
                      <ScopeChips tags={roleScope} max={8} />
                    </div>
                  )}
                  <div role="radiogroup" aria-label="Visibility scope" className="inline-flex overflow-hidden rounded-md border border-border text-xs">
                    {scopeOptions.map(([v, label]) => (
                      <button key={v} type="button" role="radio" aria-checked={scopeMode === v}
                        onClick={() => setScopeMode(v)}
                        className={cn('px-3 py-1.5 font-medium transition-colors', scopeMode === v ? 'bg-primary text-white' : 'bg-surface text-muted hover:bg-surface2')}>
                        {label}
                      </button>
                    ))}
                  </div>
                  {scopeMode === 'all'
                    ? <p className="text-xs text-muted">{roleScope.length ? 'Sees only the resources tagged with the role’s tags.' : 'Sees every device, server, service and application the role permits.'}</p>
                    : (
                      <>
                        <TagPicker value={scopeTags} onChange={setScopeTags} allowCreate={false} placeholder="Add tags to limit what this user sees…" />
                        <p className={cn('text-xs', scopeError && (submitted || scopeTags.length === 0 && false) ? 'text-danger' : 'text-muted')}>
                          {scopeError && submitted ? scopeError : roleScope.length
                            ? 'Also sees resources carrying ANY of these tags, in addition to the role’s tags.'
                            : 'Sees only resources (and their alerts) carrying ANY of these tags. Untagged resources are hidden. Clearing the last tag switches to All resources.'}
                        </p>
                      </>
                    )}
                </>
              )}
            </fieldset>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={save.isPending || (submitted && !valid) || assignable.isLoading}>
                {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                {isEdit ? 'Save changes' : 'Create user'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}
