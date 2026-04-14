import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pencil, Plus, RotateCw, Save, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { UserFormDialog } from '@/components/forms/UserFormDialog'
import { useTheme } from '@/stores/theme'
import { useAuth } from '@/stores/auth'
import { toast } from '@/components/ui/Toast'
import { Settings } from 'lucide-react'

export function SettingsPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Settings className="h-5 w-5 text-primary" />
          Settings
        </h1>
        <p className="text-xs text-muted">System and user preferences</p>
      </div>

      <Tabs defaultValue="company">
        <TabsList className="flex flex-wrap h-auto">
          <TabsTrigger value="company">Company</TabsTrigger>
          <TabsTrigger value="channels">Channels</TabsTrigger>
          <TabsTrigger value="gateways">Gateways</TabsTrigger>
          <TabsTrigger value="users">Users</TabsTrigger>
          <TabsTrigger value="snmp">SNMP Profiles</TabsTrigger>
          <TabsTrigger value="subscription">Subscription</TabsTrigger>
          <TabsTrigger value="appearance">Appearance</TabsTrigger>
          <TabsTrigger value="profile">Profile</TabsTrigger>
        </TabsList>

        <TabsContent value="company"><CompanyTab /></TabsContent>
        <TabsContent value="channels"><ChannelsTab /></TabsContent>
        <TabsContent value="gateways"><GatewaysTab /></TabsContent>
        <TabsContent value="users"><UsersTab /></TabsContent>
        <TabsContent value="snmp"><SnmpProfilesTab /></TabsContent>
        <TabsContent value="subscription"><SubscriptionTab /></TabsContent>
        <TabsContent value="appearance"><AppearanceTab /></TabsContent>
        <TabsContent value="profile"><ProfileTab /></TabsContent>
      </Tabs>
    </div>
  )
}

// --- Company ---

function CompanyTab() {
  const qc = useQueryClient()
  const { data } = useQuery<any>({
    queryKey: ['settings', 'company'],
    queryFn: async () => (await api.get('/settings/company')).data,
  })
  const [form, setForm] = useState<any>({})
  useEffect(() => {
    if (data) setForm(data)
  }, [data])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/company', form)).data,
    onSuccess: () => {
      toast.success('Company settings saved')
      qc.invalidateQueries({ queryKey: ['settings', 'company'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Company</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid grid-cols-2 gap-3">
          <FormField label="Company name" className="col-span-2">
            <Input
              value={form.company_name || ''}
              onChange={(e) => setForm({ ...form, company_name: e.target.value })}
              placeholder="Acme Corporation"
            />
          </FormField>
          <FormField label="Email">
            <Input
              type="email"
              value={form.company_email || ''}
              onChange={(e) => setForm({ ...form, company_email: e.target.value })}
            />
          </FormField>
          <FormField label="Phone">
            <Input
              value={form.company_phone || ''}
              onChange={(e) => setForm({ ...form, company_phone: e.target.value })}
            />
          </FormField>
          <FormField label="Website">
            <Input
              value={form.company_website || ''}
              onChange={(e) => setForm({ ...form, company_website: e.target.value })}
              placeholder="https://example.com"
            />
          </FormField>
          <FormField label="Timezone">
            <Select
              value={form.timezone || 'UTC'}
              onValueChange={(v) => setForm({ ...form, timezone: v })}
            >
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {['UTC', 'Asia/Riyadh', 'Asia/Dubai', 'Asia/Karachi', 'Asia/Kolkata',
                  'Asia/Shanghai', 'Asia/Tokyo', 'Europe/London', 'Europe/Paris',
                  'Europe/Berlin', 'Europe/Istanbul', 'America/New_York',
                  'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney'].map((tz) => (
                  <SelectItem key={tz} value={tz}>{tz}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Address" className="col-span-2">
            <Textarea
              value={form.company_address || ''}
              onChange={(e) => setForm({ ...form, company_address: e.target.value })}
            />
          </FormField>
          <div className="col-span-2 flex justify-end">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

// --- Channels (notification channels) ---

function ChannelsTab() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)

  const { data: channels } = useQuery<any[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => {
      const r = (await api.get('/settings/channels')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/settings/channels/${id}`),
    onSuccess: () => {
      toast.success('Channel deleted')
      qc.invalidateQueries({ queryKey: ['settings', 'channels'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const test = useMutation({
    mutationFn: async (id: string) => api.post(`/settings/channels/${id}/test`),
    onSuccess: () => toast.success('Test sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Notification channels</CardTitle>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          New channel
        </Button>
      </CardHeader>
      <CardContent>
        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Name</Th>
              <Th>Type</Th>
              <Th>Enabled</Th>
              <Th className="w-40 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {(channels || []).map((c) => (
              <Tr key={c.id}>
                <Td className="font-medium">{c.name}</Td>
                <Td><Badge variant="outline">{c.type}</Badge></Td>
                <Td>
                  <Badge variant={c.enabled ? 'success' : 'outline'}>
                    {c.enabled ? 'yes' : 'no'}
                  </Badge>
                </Td>
                <Td>
                  <div className="flex justify-end gap-1">
                    <Button size="sm" variant="outline" onClick={() => test.mutate(c.id)}>
                      Test
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => { setEditing(c); setFormOpen(true) }}>
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={() => setDeleting(c)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </Td>
              </Tr>
            ))}
            {(!channels || channels.length === 0) && (
              <Tr>
                <Td colSpan={4} className="py-8 text-center text-muted">
                  No channels yet — add one to receive notifications
                </Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </CardContent>
      <ChannelFormDialog open={formOpen} onOpenChange={setFormOpen} channel={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete channel"
        description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </Card>
  )
}

function ChannelFormDialog({
  open,
  onOpenChange,
  channel,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  channel?: any
}) {
  const isEdit = !!channel?.id
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [type, setType] = useState('email')
  const [enabled, setEnabled] = useState(true)
  const [config, setConfig] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!open) return
    if (channel) {
      setName(channel.name || '')
      setType(channel.type || 'email')
      setEnabled(channel.enabled ?? true)
      setConfig(channel.config || {})
    } else {
      setName('')
      setType('email')
      setEnabled(true)
      setConfig({})
    }
  }, [open, channel])

  const save = useMutation({
    mutationFn: async () => {
      const payload = { name, type, enabled, config }
      if (isEdit) return (await api.put(`/settings/channels/${channel.id}`, payload)).data
      return (await api.post('/settings/channels', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Channel updated' : 'Channel created')
      qc.invalidateQueries({ queryKey: ['settings', 'channels'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit channel' : 'New notification channel'}</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => { e.preventDefault(); save.mutate() }}
          className="space-y-3"
        >
          <FormField label="Name" required>
            <Input required value={name} onChange={(e) => setName(e.target.value)} />
          </FormField>
          <FormField label="Type">
            <Select value={type} onValueChange={setType}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="email">Email</SelectItem>
                <SelectItem value="sms">SMS</SelectItem>
                <SelectItem value="webhook">Webhook</SelectItem>
                <SelectItem value="slack">Slack</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {type === 'email' && (
            <FormField label="Recipients" hint="Comma-separated">
              <Input
                value={config.recipients || ''}
                onChange={(e) => setConfig({ ...config, recipients: e.target.value })}
                placeholder="ops@example.com, admin@example.com"
              />
            </FormField>
          )}
          {type === 'sms' && (
            <FormField label="Phone numbers" hint="Comma-separated, E.164">
              <Input
                value={config.numbers || ''}
                onChange={(e) => setConfig({ ...config, numbers: e.target.value })}
                placeholder="+966501234567"
              />
            </FormField>
          )}
          {type === 'webhook' && (
            <FormField label="URL" required>
              <Input
                required
                value={config.url || ''}
                onChange={(e) => setConfig({ ...config, url: e.target.value })}
                placeholder="https://hooks.example.com/endpoint"
              />
            </FormField>
          )}
          {type === 'slack' && (
            <FormField label="Slack webhook URL" required>
              <Input
                required
                value={config.webhook_url || ''}
                onChange={(e) => setConfig({ ...config, webhook_url: e.target.value })}
                placeholder="https://hooks.slack.com/services/…"
              />
            </FormField>
          )}
          {type === 'telegram' && (
            <>
              <FormField label="Bot token" required>
                <Input
                  required
                  type="password"
                  value={config.bot_token || ''}
                  onChange={(e) => setConfig({ ...config, bot_token: e.target.value })}
                />
              </FormField>
              <FormField label="Chat ID" required>
                <Input
                  required
                  value={config.chat_id || ''}
                  onChange={(e) => setConfig({ ...config, chat_id: e.target.value })}
                />
              </FormField>
            </>
          )}
          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
            <span className="text-xs font-medium uppercase tracking-wider text-muted">Enabled</span>
            <Switch checked={enabled} onCheckedChange={setEnabled} />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// --- Gateways (SMTP + SMS) ---

function GatewaysTab() {
  const { data } = useQuery<any>({
    queryKey: ['settings', 'gateways'],
    queryFn: async () => (await api.get('/settings/gateways')).data,
  })
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <SmtpGatewayCard initial={data?.smtp} />
      <SmsGatewayCard initial={data?.sms} />
    </div>
  )
}

function SmtpGatewayCard({ initial }: { initial?: any }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (initial) setForm(initial) }, [initial])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/gateways/smtp', form)).data,
    onSuccess: () => {
      toast.success('SMTP gateway saved')
      qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const test = useMutation({
    mutationFn: async () => (await api.post('/settings/gateways/smtp/test')).data,
    onSuccess: () => toast.success('Test email sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>SMTP gateway</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <FormField label="Host">
          <Input value={form.host || ''} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="smtp.example.com" />
        </FormField>
        <div className="grid grid-cols-2 gap-3">
          <FormField label="Port">
            <Input type="number" value={form.port || ''} onChange={(e) => setForm({ ...form, port: Number(e.target.value) || '' })} placeholder="587" />
          </FormField>
          <FormField label="TLS">
            <Select value={form.use_tls ? 'tls' : form.use_ssl ? 'ssl' : 'none'} onValueChange={(v) => setForm({ ...form, use_tls: v === 'tls', use_ssl: v === 'ssl' })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="tls">STARTTLS</SelectItem>
                <SelectItem value="ssl">SSL</SelectItem>
                <SelectItem value="none">None</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
        </div>
        <FormField label="Username">
          <Input value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </FormField>
        <FormField label="Password">
          <Input type="password" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder={initial?.has_password ? '••••••••' : ''} />
        </FormField>
        <FormField label="From address">
          <Input value={form.from_email || ''} onChange={(e) => setForm({ ...form, from_email: e.target.value })} placeholder="alerts@example.com" />
        </FormField>
        <div className="flex gap-2 pt-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
          <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
            Send test
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function SmsGatewayCard({ initial }: { initial?: any }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (initial) setForm(initial) }, [initial])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/gateways/sms', form)).data,
    onSuccess: () => {
      toast.success('SMS gateway saved')
      qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const test = useMutation({
    mutationFn: async () => (await api.post('/settings/gateways/sms/test')).data,
    onSuccess: () => toast.success('Test SMS sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>SMS gateway</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <FormField label="Provider">
          <Select value={form.provider || 'twilio'} onValueChange={(v) => setForm({ ...form, provider: v })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="twilio">Twilio</SelectItem>
              <SelectItem value="vonage">Vonage</SelectItem>
              <SelectItem value="http">Generic HTTP</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="Account SID / API key">
          <Input value={form.account_sid || ''} onChange={(e) => setForm({ ...form, account_sid: e.target.value })} />
        </FormField>
        <FormField label="Auth token / secret">
          <Input type="password" value={form.auth_token || ''} onChange={(e) => setForm({ ...form, auth_token: e.target.value })} placeholder={initial?.has_auth ? '••••••••' : ''} />
        </FormField>
        <FormField label="From number">
          <Input value={form.from_number || ''} onChange={(e) => setForm({ ...form, from_number: e.target.value })} placeholder="+15551234567" />
        </FormField>
        <div className="flex gap-2 pt-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </Button>
          <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
            Send test
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// --- Users ---

function UsersTab() {
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
    onSuccess: () => {
      toast.success('User deleted')
      qc.invalidateQueries({ queryKey: ['users'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const resetPw = useMutation({
    mutationFn: async () =>
      api.post(`/users/${resetTarget.id}/reset-password`, { new_password: newPass }),
    onSuccess: () => {
      toast.success('Password reset')
      setResetTarget(null)
      setNewPass('')
    },
    onError: (e: any) => toast.error('Reset failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Users</CardTitle>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          New user
        </Button>
      </CardHeader>
      <CardContent>
        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Username</Th>
              <Th>Email</Th>
              <Th>Role</Th>
              <Th>Active</Th>
              <Th>Last login</Th>
              <Th className="w-32 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {(users || []).map((u) => (
              <Tr key={u.id}>
                <Td className="font-medium">{u.username}</Td>
                <Td className="text-sm">{u.email}</Td>
                <Td><Badge variant={u.role === 'admin' ? 'success' : 'info'}>{u.role}</Badge></Td>
                <Td>
                  <Badge variant={u.is_active ? 'success' : 'outline'}>
                    {u.is_active ? 'yes' : 'no'}
                  </Badge>
                </Td>
                <Td className="text-xs text-muted">{relativeTime(u.last_login)}</Td>
                <Td>
                  <div className="flex justify-end gap-0.5">
                    <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(u); setFormOpen(true) }}>
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Reset password" onClick={() => setResetTarget(u)}>
                      <RotateCw className="h-3.5 w-3.5" />
                    </Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" onClick={() => setDeleting(u)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </Td>
              </Tr>
            ))}
            {(!users || users.length === 0) && (
              <Tr>
                <Td colSpan={6} className="py-8 text-center text-muted">No users</Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </CardContent>

      <UserFormDialog open={formOpen} onOpenChange={setFormOpen} user={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete user"
        description={<>Delete <span className="font-semibold text-text">{deleting?.username}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />

      <Dialog open={!!resetTarget} onOpenChange={(o) => !o && setResetTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Reset password</DialogTitle>
          </DialogHeader>
          <FormField label="New password" required hint="Minimum 6 characters">
            <Input
              type="password"
              minLength={6}
              value={newPass}
              onChange={(e) => setNewPass(e.target.value)}
              autoFocus
            />
          </FormField>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetTarget(null)}>Cancel</Button>
            <Button disabled={newPass.length < 6 || resetPw.isPending} onClick={() => resetPw.mutate()}>
              {resetPw.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Reset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

// --- SNMP Profiles ---

function SnmpProfilesTab() {
  const { data: profiles } = useQuery<any[]>({
    queryKey: ['snmp', 'profiles'],
    queryFn: async () => (await api.get('/snmp/profiles')).data,
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Device profiles ({profiles?.length || 0})</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Name</Th>
              <Th>Vendor</Th>
              <Th>Version</Th>
              <Th>Built-in</Th>
              <Th>Description</Th>
            </Tr>
          </THead>
          <TBody>
            {(profiles || []).map((p) => (
              <Tr key={p.id}>
                <Td className="font-medium">{p.name}</Td>
                <Td>{p.vendor || '—'}</Td>
                <Td className="text-xs">v{p.version}</Td>
                <Td>
                  <Badge variant={p.builtin ? 'success' : 'outline'}>
                    {p.builtin ? 'built-in' : 'custom'}
                  </Badge>
                </Td>
                <Td className="max-w-[400px] truncate text-xs text-muted">{p.description}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
        <p className="mt-4 text-xs text-muted">
          Profiles are loaded from <code className="rounded bg-surface2 px-1 py-0.5">/opt/zenplus/data/profiles/*.json</code> on poller startup.
        </p>
      </CardContent>
    </Card>
  )
}

// --- Subscription ---

function SubscriptionTab() {
  const qc = useQueryClient()
  const { data } = useQuery<any>({
    queryKey: ['subscription'],
    queryFn: async () => (await api.get('/subscription')).data,
  })
  const refresh = useMutation({
    mutationFn: async () => (await api.post('/subscription/refresh-subscription')).data,
    onSuccess: () => {
      toast.success('Subscription refreshed')
      qc.invalidateQueries({ queryKey: ['subscription'] })
    },
    onError: (e: any) => toast.error('Refresh failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Subscription</CardTitle>
        <Button variant="outline" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          <RotateCw className={`h-4 w-4 ${refresh.isPending ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <Row k="Plan" v={data?.plan || 'trial'} />
        <Row k="Status" v={data?.status || '—'} />
        <Row k="Started" v={data?.started_at || '—'} />
        <Row k="Expires" v={data?.expires_at || '—'} />
        <Row k="Max devices" v={String(data?.max_devices ?? '—')} />
        <Row k="Max service checks" v={String(data?.max_service_checks ?? '—')} />
        <Row k="Max users" v={String(data?.max_users ?? '—')} />
      </CardContent>
    </Card>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
      <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
      <span>{v}</span>
    </div>
  )
}

// --- Appearance ---

function AppearanceTab() {
  const { theme, set } = useTheme()
  return (
    <Card>
      <CardHeader>
        <CardTitle>Appearance</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Theme</div>
          <div className="grid grid-cols-2 gap-3 max-w-md">
            <button
              onClick={() => set('dark')}
              className={`rounded-lg border-2 p-4 text-left transition ${theme === 'dark' ? 'border-primary' : 'border-border'}`}
            >
              <div className="mb-2 h-16 rounded bg-[rgb(7,10,16)]"></div>
              <div className="text-sm font-medium">Dark (NOC)</div>
              <div className="text-xs text-muted">High contrast, easy on eyes</div>
            </button>
            <button
              onClick={() => set('light')}
              className={`rounded-lg border-2 p-4 text-left transition ${theme === 'light' ? 'border-primary' : 'border-border'}`}
            >
              <div className="mb-2 h-16 rounded bg-[rgb(248,250,252)]"></div>
              <div className="text-sm font-medium">Light</div>
              <div className="text-xs text-muted">Daylight reading</div>
            </button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

// --- Profile ---

function ProfileTab() {
  const { user } = useAuth()
  return (
    <Card>
      <CardHeader>
        <CardTitle>Your profile</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <Row k="Username" v={user?.username || '—'} />
        <Row k="Email" v={user?.email || '—'} />
        <Row k="Full name" v={user?.full_name || '—'} />
        <Row k="Role" v={user?.role || '—'} />
      </CardContent>
    </Card>
  )
}
