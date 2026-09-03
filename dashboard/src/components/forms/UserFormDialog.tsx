import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { Role, User } from '@/types'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { TagPicker } from '@/components/tags/TagPicker'
import { useAuth } from '@/stores/auth'

const SOURCE_LABEL: Record<string, string> = { ldap: 'LDAP / Active Directory', radius: 'RADIUS' }

export function UserFormDialog({
  open,
  onOpenChange,
  user,
}: {
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

  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
    enabled: open,
  })

  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('viewer')
  const [isActive, setIsActive] = useState(true)
  const [scopeTags, setScopeTags] = useState<string[]>([])

  useEffect(() => {
    if (!open) return
    setUsername(user?.username || '')
    setEmail(user?.email || '')
    setFullName(user?.full_name || '')
    setPassword('')
    setRole(user?.role || 'viewer')
    setIsActive(user?.is_active ?? true)
    setScopeTags(user?.scope_tags || [])
  }, [open, user])

  const selectedRole = (roles || []).find((r) => r.name === role)
  const roleIsAdmin = !!selectedRole?.permissions.includes('system.admin')

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        const payload: Record<string, unknown> = { email, full_name: fullName || null, is_active: isActive, scope_tags: scopeTags }
        if (!isExternal) payload.role = role
        return (await api.put(`/users/${user!.id}`, payload)).data
      }
      return (
        await api.post('/users', {
          username: username.trim(),
          email,
          full_name: fullName || null,
          password,
          role,
          scope_tags: scopeTags,
        })
      ).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'User updated' : 'User created')
      qc.invalidateQueries({ queryKey: ['users'] })
      qc.invalidateQueries({ queryKey: ['roles'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit user: ${user?.username}` : 'New local user'}</DialogTitle>
          <DialogDescription>
            {isExternal
              ? `This account signs in through ${SOURCE_LABEL[source]}. Its password and role are managed by the directory mapping on every sign-in.`
              : isEdit
                ? 'Local account with a password stored on this appliance.'
                : 'Creates a local account. Directory users are created automatically on their first sign-in.'}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FormField label="Username" required hint={isEdit ? undefined : '3–100 characters, used to sign in'}>
              <Input
                required
                minLength={3}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={isEdit}
                placeholder="jsmith"
                autoComplete="off"
                autoFocus={!isEdit}
              />
            </FormField>
            <FormField label="Full name">
              <Input value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="John Smith" />
            </FormField>
          </div>
          <FormField label="Email" required>
            <Input required type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="user@example.com" autoComplete="off" />
          </FormField>
          {!isEdit && (
            <FormField label="Password" required hint="Minimum 6 characters">
              <PasswordInput required minLength={6} value={password} onChange={(e) => setPassword(e.target.value)} />
            </FormField>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FormField label="Role" hint={isExternal ? 'Set by the directory mapping' : isSelf ? 'You cannot change your own role.' : roleIsAdmin ? 'Administrators see everything and bypass visibility scope.' : undefined}>
              <Select value={role} onValueChange={setRole} disabled={isExternal || isSelf}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(roles && roles.length > 0
                    ? roles.map((r) => ({ value: r.name, label: r.display_name }))
                    : [{ value: role, label: role }]
                  ).map((r) => (
                    <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            {isEdit && (
              <FormField label="Status" hint={isSelf ? 'You cannot disable your own account.' : undefined}>
                <label className="flex h-9 cursor-pointer items-center justify-between rounded-md border border-border px-3">
                  <span className="text-sm">{isActive ? 'Active' : 'Disabled'}</span>
                  <Switch checked={isActive} onCheckedChange={setIsActive} disabled={isSelf} />
                </label>
              </FormField>
            )}
          </div>

          <FormField
            label="Visibility scope"
            hint={roleIsAdmin
              ? 'Not applied to administrators.'
              : scopeTags.length === 0
                ? 'Unrestricted: this user sees every device, server, service, and application.'
                : 'Sees only devices, servers, services, applications, and their alerts carrying at least one of these tags. Untagged entities are hidden.'}
          >
            <TagPicker value={scopeTags} onChange={setScopeTags} allowCreate={false}
              placeholder="Unrestricted — add tags to limit visibility…" />
          </FormField>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create user'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
