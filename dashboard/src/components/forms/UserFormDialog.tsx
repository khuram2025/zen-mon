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
const SOURCE_SHORT: Record<string, string> = { ldap: 'LDAP', radius: 'RADIUS', oidc: 'SSO' }
const EMAIL_RE = /^[^@\s<>()[\],;:\\"]+@[^@\s<>()[\],;:\\"]+\.[^@\s<>()[\],;:\\"]{2,}$/

type Assignable = { name: string; display_name: string; description: string; permission_count: number; privileged: boolean; page_limited?: boolean }

/** Create or edit a user.
 *
 *  - only roles the signed-in account may assign are offered (the server
 *    enforces the same ceiling);
 *  - edit sends only what changed, so a name edit never revokes sessions;
 *  - a directory account (LDAP / RADIUS / OIDC) takes its role from the provider's
 *    role mapping at every sign-in, or keeps a role set here by hand; the directory
 *    always decides whether the account may sign in at all;
 *  - the visibility scope is an explicit choice (All resources / Selected tags)
 *    and is shown as locked for administrators, who bypass it;
 *  - password rules come from the API, so the form never promises less. */
export function UserFormDialog({ open, onOpenChange, user }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  user?: User | null
}) {
  const isEdit = !!user?.id
  const [newSource, setNewSource] = useState<'local' | 'ldap'>('local')
  const source = isEdit ? user?.auth_source || 'local' : newSource
  const isExternal = source !== 'local'
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
  // Directory accounts: who decides the role (the provider's mapping, or this form).
  const [roleSource, setRoleSource] = useState<'directory' | 'manual'>('directory')
  const [isActive, setIsActive] = useState(true)
  const [scopeMode, setScopeMode] = useState<'all' | 'tags'>('all')
  const [scopeTags, setScopeTags] = useState<string[]>([])
  const [mustChange, setMustChange] = useState(true)
  const [errors, setErrors] = useState<{ username?: string; email?: string; general?: string; password?: string }>({})
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    setNewSource('local')
    setUsername(user?.username || '')
    setEmail(user?.email || '')
    setFullName(user?.full_name || '')
    setPassword(''); setPassword2('')
    setRole(user?.role || 'viewer')
    setRoleSource(user?.role_source === 'manual' ? 'manual' : 'directory')
    setIsActive(user?.is_active ?? true)
    setScopeTags(user?.scope_tags || [])
    setScopeMode((user?.scope_tags || []).length > 0 ? 'tags' : 'all')
    setMustChange(true)
    setErrors({}); setSubmitted(false)
  }, [open, user])

  // A delegated manager may not be allowed to assign Viewer: start a new user on a role they can assign,
  // instead of an empty select that fails with 403 on save.
  useEffect(() => {
    if (!open || isEdit || !assignable.data?.length) return
    if (!assignable.data.some((r) => r.name === role)) setRole(assignable.data[0].name)
  }, [open, isEdit, assignable.data]) // eslint-disable-line react-hooks/exhaustive-deps

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
  const storedSource = user?.role_source === 'manual' ? 'manual' : 'directory'
  const sourceChanged = isExternal && !isSelf && roleSource !== storedSource
  // The role is editable for a local account, and for a directory account once it is set here.
  const roleFromDirectory = isEdit && isExternal && roleSource === 'directory'
  const roleChanged = !isSelf && !roleFromDirectory && role !== user?.role
  const directoryRole = user?.directory_role || null
  const directoryRoleLabel = directoryRole ? roleMeta[directoryRole]?.display_name || directoryRole : null
  const sourceShort = SOURCE_SHORT[source] || 'directory'

  function chooseRoleSource(next: 'directory' | 'manual') {
    setRoleSource(next)
    // Following the directory shows what it gives (when known); setting by hand starts from the stored role.
    setRole(next === 'directory' ? (storedSource === 'manual' && directoryRole ? directoryRole : user?.role || 'viewer') : (user?.role || 'viewer'))
  }

  const pwCheck = usePasswordCheck(password, password2, { username: username.trim() || user?.username, email })
  const emailError = !email.trim() ? 'Email is required' : !EMAIL_RE.test(email.trim()) ? 'Enter a valid email address' : undefined
  const usernameError = isEdit ? undefined : username.trim().length < 3 ? 'Username must be at least 3 characters' : username.trim().length > 100 ? 'Username can be at most 100 characters' : undefined
  const scopeError = scopeMode === 'tags' && scopeTags.length === 0 ? 'Choose at least one tag, or switch to “All resources”' : undefined
  const valid = !emailError && !usernameError && !scopeError && (isEdit || isExternal || pwCheck.ok)

  const effectiveScope = scopeMode === 'all' ? [] : scopeTags
  const dirty = isEdit
    ? email.trim().toLowerCase() !== (user?.email || '').toLowerCase()
      || (fullName.trim() || null) !== (user?.full_name || null)
      || roleChanged || sourceChanged
      || isActive !== (user?.is_active ?? true)
      || JSON.stringify([...effectiveScope].sort()) !== JSON.stringify([...(user?.scope_tags || [])].sort())
    : !!(username || email || fullName || password || newSource !== 'local')

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        // Only what changed: unchanged access fields would needlessly revoke sessions.
        const payload: Record<string, unknown> = {}
        if (email.trim().toLowerCase() !== (user!.email || '').toLowerCase()) payload.email = email.trim()
        if ((fullName.trim() || null) !== (user!.full_name || null)) payload.full_name = fullName.trim() || null
        if (roleChanged) payload.role = role
        if (sourceChanged) payload.role_source = roleSource
        if (isActive !== (user!.is_active ?? true)) payload.is_active = isActive
        if (JSON.stringify([...effectiveScope].sort()) !== JSON.stringify([...(user!.scope_tags || [])].sort())) payload.scope_tags = effectiveScope
        if (Object.keys(payload).length === 0) return { unchanged: true }
        return (await api.put(`/users/${user!.id}`, payload)).data
      }
      return (await api.post('/users', {
        username: username.trim(), email: email.trim(), full_name: fullName.trim() || null,
        auth_source: newSource, role, scope_tags: effectiveScope,
        ...(isExternal ? {} : { password, must_change_password: mustChange }),
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
            <DialogTitle>{isEdit ? `Edit user: ${user?.username}` : 'New user'}</DialogTitle>
            <DialogDescription>
              {isExternal
                ? (isEdit ? `This account signs in through ${SOURCE_LABEL[source]}. Review its role and access below.`
                  : 'Enter the directory username and assign a ZenPlus role. The user signs in with their directory password.')
                : isEdit
                  ? 'Local account with a password stored on this appliance.'
                  : 'Choose a sign-in source and assign a ZenPlus role.'}
            </DialogDescription>
          </DialogHeader>
          <form noValidate onSubmit={submit} className="space-y-4">
            {errors.general && <div role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">{errors.general}</div>}

            {!isEdit && (
              <FormField label="Sign-in source">
                <Select value={newSource} onValueChange={(v: 'local' | 'ldap') => { setNewSource(v); setErrors({}) }}>
                  <SelectTrigger aria-label="Sign-in source"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="local">Local</SelectItem>
                    <SelectItem value="ldap">LDAP / Active Directory</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            )}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="Username" required hint={isEdit ? 'Usernames cannot be changed.' : '3–100 characters. Used to sign in; unique ignoring case.'} error={(submitted || username) ? (errors.username || usernameError) : errors.username}>
                <Input value={username} onChange={(e) => { setUsername(e.target.value); setErrors({}) }} disabled={isEdit} placeholder="jsmith" autoComplete="off" autoFocus={!isEdit} aria-invalid={!!(errors.username || usernameError)} />
              </FormField>
              <FormField label="Full name" hint={isExternal && source !== 'radius' ? 'Updated from the directory at each sign-in when it supplies a name.' : undefined}>
                <Input value={fullName} maxLength={255} onChange={(e) => setFullName(e.target.value)} placeholder="John Smith" />
              </FormField>
            </div>
            <FormField label="Email" required hint={isExternal && source !== 'radius' ? 'Updated from the directory at each sign-in when it supplies an address.' : undefined} error={(submitted || email) ? (errors.email || emailError) : errors.email}>
              <Input type="email" value={email} onChange={(e) => { setEmail(e.target.value); setErrors({}) }} placeholder="user@example.com" autoComplete="off" aria-invalid={!!(errors.email || emailError)} />
            </FormField>

            {!isEdit && !isExternal && (
              <>
                <NewPasswordFields password={password} onPassword={(v) => { setPassword(v); setErrors({}) }} confirm={password2} onConfirm={setPassword2} ctx={{ username, email }} label="Initial password" />
                {errors.password && <p role="alert" className="-mt-2 text-xs text-danger">{errors.password}</p>}
                <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2">
                  <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-primary" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
                  <span className="text-xs"><span className="font-medium">Require a new password at first sign-in</span><br /><span className="text-muted">Recommended: you chose this password, so only the user should keep using it.</span></span>
                </label>
              </>
            )}

            {isEdit && isExternal && (
              <fieldset className="space-y-1.5">
                <legend className="mb-1 text-sm font-medium">Role is decided by</legend>
                <div role="radiogroup" aria-label="Role is decided by" className="inline-flex overflow-hidden rounded-md border border-border text-xs">
                  {([['directory', `${sourceShort} role mapping`], ['manual', 'An administrator (set here)']] as const).map(([v, label]) => (
                    <button key={v} type="button" role="radio" aria-checked={roleSource === v} disabled={isSelf || roleAboveYou}
                      onClick={() => chooseRoleSource(v)}
                      className={cn('px-3 py-1.5 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60', roleSource === v ? 'bg-primary text-white' : 'bg-surface text-muted hover:bg-surface2')}>
                      {label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-muted">
                  {roleSource === 'directory'
                    ? storedSource === 'manual'
                      ? directoryRoleLabel
                        ? `Saving applies the role the mapping gives this account (${directoryRoleLabel}) and signs it out. From then on the mapping is applied at every sign-in.`
                        : 'The role the mapping gives this account is not known yet. Saving signs the account out; it takes the mapped role at its next sign-in.'
                      : 'The role mapping in Authentication is applied at every sign-in. Choose “An administrator” to give this account a different role.'
                    : `Sign-in keeps the selected role. ${source === 'ldap' ? 'In listed-accounts mode, this role also grants access after directory authentication.' : 'The provider’s role mapping still controls admission.'}`}
                </p>
              </fieldset>
            )}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField
                label="Role"
                hint={
                  isSelf ? 'You cannot change your own role.'
                  : roleFromDirectory && !roleAboveYou ? `From the ${sourceShort} role mapping.`
                  : roleAboveYou ? 'This account has more access than you do, so only a higher administrator can change it.'
                  : roleIsAdmin ? 'Full administration: sees everything and bypasses visibility scope.'
                  : roleMeta[role]?.description || undefined
                }
              >
                {/* Radix reports '' while the options are still loading; never store it as the role. */}
                <Select value={role} onValueChange={(v) => { if (v) setRole(v) }} disabled={roleFromDirectory || isSelf || roleAboveYou || assignable.isLoading}>
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
            {roleOptions.some((r) => r.name === role && r.page_limited) && (
              <p role="note" className="flex items-start gap-2 text-xs text-muted"><Info className="mt-0.5 h-3.5 w-3.5 shrink-0" /> This role is limited to selected pages and network maps; the user lands on the first of them.</p>
            )}
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
