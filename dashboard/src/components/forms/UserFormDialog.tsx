import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { Role } from '@/types'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
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

export function UserFormDialog({
  open,
  onOpenChange,
  user,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  user?: any
}) {
  const isEdit = !!user?.id
  const isExternal = isEdit && user?.auth_source && user.auth_source !== 'local'
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

  useEffect(() => {
    if (!open) return
    if (user) {
      setUsername(user.username || '')
      setEmail(user.email || '')
      setFullName(user.full_name || '')
      setPassword('')
      setRole(user.role || 'viewer')
      setIsActive(user.is_active ?? true)
    } else {
      setUsername('')
      setEmail('')
      setFullName('')
      setPassword('')
      setRole('viewer')
      setIsActive(true)
    }
  }, [open, user])

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        const payload: any = { email, full_name: fullName || null, role, is_active: isActive }
        return (await api.put(`/users/${user.id}`, payload)).data
      }
      return (
        await api.post('/users', {
          username,
          email,
          full_name: fullName || null,
          password,
          role,
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
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit user' : 'New user'}</DialogTitle>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <FormField label="Username" required>
            <Input
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={isEdit}
              placeholder="jsmith"
            />
          </FormField>
          <FormField label="Email" required>
            <Input
              required
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@example.com"
            />
          </FormField>
          <FormField label="Full name">
            <Input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="John Smith"
            />
          </FormField>
          {!isEdit && (
            <FormField label="Password" required hint="Minimum 6 characters">
              <Input
                required
                type="password"
                minLength={6}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </FormField>
          )}
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Role" hint={isExternal ? `Managed by ${user.auth_source.toUpperCase()} mapping on each sign-in` : undefined}>
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(roles && roles.length > 0
                    ? roles.map((r) => ({ value: r.name, label: r.display_name }))
                    : [
                        { value: 'admin', label: 'Administrator' },
                        { value: 'operator', label: 'Operator' },
                        { value: 'viewer', label: 'Viewer' },
                        { value: 'read_only', label: 'Read Only' },
                      ]
                  ).map((r) => (
                    <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            {isEdit && (
              <div className="flex items-center justify-between rounded-md border border-border px-3">
                <span className="text-xs font-medium uppercase tracking-wider text-muted">Active</span>
                <Switch checked={isActive} onCheckedChange={setIsActive} />
              </div>
            )}
          </div>
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
