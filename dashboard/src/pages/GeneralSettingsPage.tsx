import { FormEvent, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Building2, Download, HardDrive, KeyRound, LayoutTemplate, LifeBuoy, Loader2, Mail, Palette, Plug, Save, Send, Settings, ShieldCheck, Upload, User as UserIcon, Users } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useTheme } from '@/stores/theme'
import { useAuth, useCan } from '@/stores/auth'
import { toast } from '@/components/ui/Toast'
import { UpdatesTabContent } from '@/components/UpdatesTabContent'
import { LicensesTabContent } from '@/components/LicensesTabContent'
import { SensorsCard } from '@/components/SensorsCard'
import { SupportTabContent } from '@/components/SupportTabContent'
import { StorageTabContent } from '@/components/StorageTabContent'
import { SecurityTabContent } from '@/components/SecurityTabContent'
import { SshTerminalCard } from '@/components/settings/SshTerminalCard'
import { CompanySettings } from '@/components/settings/CompanySettings'
import { MibsTabContent } from '@/components/MibsTabContent'
import { TemplatesTabContent } from '@/components/TemplatesTabContent'
import { AccessTabContent, ACCESS_TAB_PERMISSIONS } from '@/components/access/AccessTabContent'
import { MfaPanel } from '@/components/security/MfaPanel'
import { NewPasswordFields, usePasswordCheck } from '@/components/security/PasswordFields'
import { QueryBoundary } from '@/components/ui/QueryState'

// `permission`: hidden unless the user's role grants it (system.admin
// always passes). Tabs without one are visible to everyone.
const TABS = [
  { value: 'company', label: 'Company', icon: Building2, permission: 'settings.manage' },
  { value: 'monitoring', label: 'Monitoring', icon: Activity, permission: 'settings.manage' },
  { value: 'smtp', label: 'SMTP / Email', icon: Mail, permission: 'settings.manage' },
  { value: 'appearance', label: 'Appearance', icon: Palette },
  { value: 'users', label: 'Users & Access', icon: Users, permission: ACCESS_TAB_PERMISSIONS },
  { value: 'licenses', label: 'Licenses', icon: KeyRound, permission: 'system.admin' },
  { value: 'updates', label: 'Updates', icon: Download, permission: 'system.admin' },
  { value: 'storage', label: 'Storage', icon: HardDrive, permission: 'system.admin' },
  { value: 'security', label: 'Security', icon: ShieldCheck, permission: 'system.admin' },
  { value: 'sensors', label: 'Sensors', icon: Plug, permission: 'settings.manage' },
  { value: 'templates', label: 'Monitoring Templates', icon: LayoutTemplate, permission: 'settings.manage' },
  { value: 'mibs', label: 'MIB Library', icon: Upload, permission: 'settings.manage' },
  { value: 'support', label: 'Support', icon: LifeBuoy },
  { value: 'profile', label: 'Profile', icon: UserIcon },
] as const

type TabValue = typeof TABS[number]['value']

export function GeneralSettingsPage() {
  const can = useCan()
  const visibleTabs = TABS.filter((t) => {
    if (!('permission' in t) || !t.permission) return true
    const needed = (Array.isArray(t.permission) ? t.permission : [t.permission]) as readonly string[]
    return needed.some((p) => can(p))
  })
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.get('tab') as TabValue | null
  const fallback: TabValue = visibleTabs.some((t) => t.value === 'company') ? 'company' : (visibleTabs[0]?.value ?? 'profile')
  const active: TabValue = visibleTabs.some((t) => t.value === requested) ? (requested as TabValue) : fallback

  function setActive(v: string) {
    const next = new URLSearchParams(searchParams)
    if (v === 'company') next.delete('tab')
    else next.set('tab', v)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Settings className="h-5 w-5 text-primary" /> General Settings
        </h1>
        <p className="text-xs text-muted">
          Company information, email, appearance, users &amp; access control, licenses, updates, sensors, monitoring templates, MIBs, and your profile
        </p>
      </div>

      <Tabs value={active} onValueChange={setActive}>
        <TabsList className="h-auto flex-wrap gap-1 bg-surface2/50 p-1">
          {visibleTabs.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="gap-1.5">
              <t.icon className="h-3.5 w-3.5" />
              <span>{t.label}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="company">
          <CompanySettings />
        </TabsContent>
        <TabsContent value="monitoring" className="space-y-4">
          <MonitoringCard />
          <SshTerminalCard />
        </TabsContent>
        <TabsContent value="users">
          <AccessTabContent />
        </TabsContent>
        <TabsContent value="smtp">
          <SmtpCard />
        </TabsContent>
        <TabsContent value="appearance">
          <AppearanceCard />
        </TabsContent>
        <TabsContent value="licenses">
          <LicensesTabContent />
        </TabsContent>
        <TabsContent value="updates">
          <UpdatesTabContent />
        </TabsContent>
        <TabsContent value="storage">
          <StorageTabContent />
        </TabsContent>
        <TabsContent value="security">
          <SecurityTabContent />
        </TabsContent>
        <TabsContent value="sensors">
          <SensorsCard />
        </TabsContent>
        <TabsContent value="templates">
          <TemplatesTabContent />
        </TabsContent>
        <TabsContent value="mibs">
          <MibsTabContent />
        </TabsContent>
        <TabsContent value="support">
          <SupportTabContent />
        </TabsContent>
        <TabsContent value="profile">
          <ProfileCard />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function MonitoringCard() {
  const qc = useQueryClient()
  const { data } = useQuery<any>({
    queryKey: ['settings', 'monitoring'],
    queryFn: async () => (await api.get('/settings/monitoring')).data,
  })
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (data) setForm(data) }, [data])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/monitoring', {
      degraded_rtt_ms: Number(form.degraded_rtt_ms),
      degraded_loss_pct: Number(form.degraded_loss_pct),
    })).data,
    onSuccess: () => { toast.success('Monitoring thresholds saved', 'The poller applies them within about a minute.'); qc.invalidateQueries({ queryKey: ['settings', 'monitoring'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader><CardTitle>Degraded thresholds</CardTitle></CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-muted">
          A device that still answers ping is marked <span className="font-medium text-text">Degraded</span> when
          either its round-trip time OR packet loss is strictly above the configured limit. Equality does not trigger degradation.
          Each ping check is evaluated individually; chart averages do not determine this status. Changes apply after the controller configuration refresh (normally within a minute) and a subsequent check. Remote sensor results use these settings when received.
          CPU, memory and hardware alerts use their own rules.
        </p>
        <form onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate() }} className="grid grid-cols-2 gap-3">
          <FormField label="Round-trip time above" hint="Latency beyond which a responding device counts as degraded (default 100 ms)">
            <div className="flex items-center gap-2">
              <Input type="number" min={1} max={10000} value={form.degraded_rtt_ms ?? ''} onChange={(e) => setForm({ ...form, degraded_rtt_ms: e.target.value })} className="w-32" />
              <span className="text-sm text-muted">ms</span>
            </div>
          </FormField>
          <FormField label="Packet loss above" hint="Loss beyond which a responding device counts as degraded (default 10%)">
            <div className="flex items-center gap-2">
              <Input type="number" min={1} max={100} value={form.degraded_loss_pct ?? ''} onChange={(e) => setForm({ ...form, degraded_loss_pct: e.target.value })} className="w-32" />
              <span className="text-sm text-muted">%</span>
            </div>
          </FormField>
          <div className="col-span-2 flex justify-end">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function AppearanceCard() {
  const { theme, set } = useTheme()
  return (
    <Card>
      <CardHeader><CardTitle>Appearance</CardTitle></CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3">
          <button onClick={() => set('dark')} className={`rounded-lg border-2 p-3 text-left transition ${theme === 'dark' ? 'border-primary' : 'border-border'}`}>
            <div className="mb-2 h-12 rounded bg-[rgb(7,10,16)]" />
            <div className="text-sm font-medium">Dark (NOC)</div>
            <div className="text-xs text-muted">High contrast</div>
          </button>
          <button onClick={() => set('light')} className={`rounded-lg border-2 p-3 text-left transition ${theme === 'light' ? 'border-primary' : 'border-border'}`}>
            <div className="mb-2 h-12 rounded bg-[rgb(248,250,252)]" />
            <div className="text-sm font-medium">Light</div>
            <div className="text-xs text-muted">Daylight reading</div>
          </button>
        </div>
      </CardContent>
    </Card>
  )
}

function ProfileCard() {
  const { user, logout } = useAuth()
  const qc = useQueryClient()
  const [currentPass, setCurrentPass] = useState('')
  const [newPass, setNewPass] = useState('')
  const [confirmPass, setConfirmPass] = useState('')
  const isLocal = (user?.auth_source || 'local') === 'local'
  const check = usePasswordCheck(newPass, confirmPass, { username: user?.username, email: user?.email })

  const changePw = useMutation({
    mutationFn: async () =>
      (await api.post('/users/me/change-password', { current_password: currentPass, new_password: newPass })).data,
    onSuccess: () => {
      // The API signs every session out on a password change, this one included.
      toast.success('Password changed', 'Sign in again with your new password.')
      setCurrentPass(''); setNewPass(''); setConfirmPass('')
      logout()
    },
    onError: (e: any) => toast.error('Change failed', apiErrorMessage(e)),
  })

  type MySession = { id: string; created_at: string; last_seen_at: string; ip_address: string | null; user_agent: string | null; auth_method: string; mfa_verified: boolean }
  const sessions = useQuery<{ data: MySession[] }>({
    queryKey: ['users', 'me', 'sessions'],
    queryFn: async () => (await api.get('/users/me/sessions')).data,
  })
  const revokeSession = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/me/sessions/${id}`),
    onSuccess: () => { toast.success('Session revoked'); qc.invalidateQueries({ queryKey: ['users', 'me', 'sessions'] }) },
    onError: (e: any) => toast.error('Could not revoke', apiErrorMessage(e)),
  })

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Your Profile</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          {[
            ['Username', user?.username || '—'],
            ['Email', user?.email || '—'],
            ['Full name', user?.full_name || '—'],
            ['Role', user?.role || '—'],
            ['Sign-in', (user?.auth_source || 'local').toUpperCase()],
            ['Visibility', (() => {
              if ((user?.permissions || []).includes('system.admin')) return 'All resources (full administration)'
              const own = user?.scope_tags || []
              const fromRole = (user?.role_scope_tags || []).filter((t) => !own.some((o) => o.toLowerCase() === t.toLowerCase()))
              const all = [...fromRole, ...own]
              return all.length ? `Tags: ${all.join(', ')}${fromRole.length ? ' (role tags included)' : ''}` : 'All resources your role permits'
            })()],
          ].map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
              <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
              <span className="text-right">{v}</span>
            </div>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Change Password</CardTitle></CardHeader>
        <CardContent>
          {isLocal ? (
            <form
              onSubmit={(e: FormEvent) => { e.preventDefault(); if (check.ok && currentPass) changePw.mutate() }}
              className="space-y-3"
            >
              <FormField label="Current password" required>
                <Input required type="password" value={currentPass} onChange={(e) => setCurrentPass(e.target.value)} autoComplete="current-password" />
              </FormField>
              <NewPasswordFields password={newPass} onPassword={setNewPass} confirm={confirmPass} onConfirm={setConfirmPass} ctx={{ username: user?.username, email: user?.email }} />
              <p className="text-[11px] text-muted">Changing your password signs you out of every session, including this one.</p>
              <div className="flex justify-end">
                <Button type="submit" disabled={changePw.isPending || !currentPass || !check.ok}>
                  {changePw.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Change password
                </Button>
              </div>
            </form>
          ) : (
            <p className="text-sm text-muted">
              Your account signs in via {(user?.auth_source || '').toUpperCase()}; your password is managed by that directory.
            </p>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Two-step verification</CardTitle></CardHeader>
        <CardContent><MfaPanel /></CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Where you are signed in</CardTitle></CardHeader>
        <CardContent>
          <QueryBoundary query={sessions} what="your sessions" skeleton={<div className="h-16 animate-pulse rounded bg-surface2/50" />}>
            {(s) => s.data.length === 0 ? (
              <p className="text-sm text-muted">No tracked sessions. (Sessions started before this release are not listed.)</p>
            ) : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {s.data.map((row) => {
                  const current = row.id === user?.session_id
                  return (
                    <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs">
                      <div className="min-w-0">
                        <div className="font-medium">{row.ip_address || 'unknown address'} {current && <span className="ml-1 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">This session</span>}</div>
                        <div className="truncate text-muted" title={row.user_agent || ''}>{row.user_agent || 'unknown client'}</div>
                        <div className="text-muted">Started {new Date(row.created_at).toLocaleString()} · last seen {relativeTime(row.last_seen_at)}</div>
                      </div>
                      {!current && <Button size="sm" variant="outline" disabled={revokeSession.isPending} onClick={() => revokeSession.mutate(row.id)}>Revoke</Button>}
                    </li>
                  )
                })}
              </ul>
            )}
          </QueryBoundary>
        </CardContent>
      </Card>
    </div>
  )
}

interface SmtpForm {
  enabled: boolean
  host: string
  port: number | ''
  encryption: 'tls' | 'ssl' | 'none'
  username: string
  password: string
  from_email: string
  from_name: string
}

const DEFAULT_SMTP: SmtpForm = {
  enabled: false,
  host: '',
  port: 587,
  encryption: 'tls',
  username: '',
  password: '',
  from_email: '',
  from_name: 'ZenPlus Alerts',
}

function SmtpCard() {
  const qc = useQueryClient()
  const { user } = useAuth()
  const { data, isLoading } = useQuery<{ smtp?: Partial<SmtpForm> }>({
    queryKey: ['settings', 'gateways'],
    queryFn: async () => (await api.get('/settings/gateways')).data,
  })
  const [form, setForm] = useState<SmtpForm>(DEFAULT_SMTP)
  const [testRecipient, setTestRecipient] = useState('')

  useEffect(() => {
    if (data?.smtp) {
      setForm({
        ...DEFAULT_SMTP,
        ...data.smtp,
        port: (data.smtp.port as number) ?? 587,
        encryption: (data.smtp.encryption as 'tls' | 'ssl' | 'none') ?? 'tls',
      })
    }
  }, [data])

  useEffect(() => {
    if (!testRecipient && user?.email) setTestRecipient(user.email)
  }, [user, testRecipient])

  const save = useMutation({
    mutationFn: async () => {
      const payload = { ...form, port: Number(form.port) || 587 }
      return (await api.put('/settings/gateways/smtp', payload)).data
    },
    onSuccess: () => {
      toast.success('SMTP settings saved')
      qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const test = useMutation({
    mutationFn: async () => {
      if (!testRecipient) throw new Error('Recipient required')
      return (await api.post('/settings/gateways/smtp/test', { recipient: testRecipient })).data
    },
    onSuccess: (d: any) => toast.success('Test email sent', d?.message ?? `Check inbox for ${testRecipient}`),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-primary" />
          SMTP / Email
        </CardTitle>
        <p className="text-xs text-muted">
          Outbound email server used for alerts, scheduled reports, password resets, and other notifications.
          Also configurable from the Gateways page.
        </p>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate() }}
          className="grid grid-cols-1 gap-3 md:grid-cols-2"
        >
          <div className="md:col-span-2 flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
            <div>
              <p className="text-sm font-medium">Enabled</p>
              <p className="text-[11px] text-muted">When off, the system won't attempt to send any email.</p>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(v) => setForm((f) => ({ ...f, enabled: !!v }))}
            />
          </div>

          <FormField label="SMTP host" className="md:col-span-2">
            <Input
              value={form.host}
              onChange={(e) => setForm({ ...form, host: e.target.value })}
              placeholder="smtp.gmail.com"
            />
          </FormField>

          <FormField label="Port">
            <Input
              type="number"
              value={form.port === '' ? '' : form.port}
              onChange={(e) => setForm({ ...form, port: e.target.value === '' ? '' : Number(e.target.value) })}
              placeholder="587"
            />
          </FormField>

          <FormField label="Encryption">
            <Select
              value={form.encryption}
              onValueChange={(v) => setForm({ ...form, encryption: v as 'tls' | 'ssl' | 'none' })}
            >
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="tls">STARTTLS (587)</SelectItem>
                <SelectItem value="ssl">SSL (465)</SelectItem>
                <SelectItem value="none">None (25)</SelectItem>
              </SelectContent>
            </Select>
          </FormField>

          <FormField label="Username">
            <Input
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="alerts@example.com"
              autoComplete="off"
            />
          </FormField>

          <FormField label="Password / App password">
            <Input
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              placeholder={data?.smtp?.password ? '••••••••' : ''}
              autoComplete="new-password"
            />
          </FormField>

          <FormField label="From address">
            <Input
              type="email"
              value={form.from_email}
              onChange={(e) => setForm({ ...form, from_email: e.target.value })}
              placeholder="alerts@example.com"
            />
          </FormField>

          <FormField label="From display name">
            <Input
              value={form.from_name}
              onChange={(e) => setForm({ ...form, from_name: e.target.value })}
              placeholder="ZenPlus Alerts"
            />
          </FormField>

          <div className="md:col-span-2 mt-2 flex flex-col gap-2 border-t border-border pt-3 md:flex-row md:items-end md:justify-between">
            <div className="flex flex-1 items-end gap-2">
              <FormField label="Send test email to" className="flex-1">
                <Input
                  type="email"
                  value={testRecipient}
                  onChange={(e) => setTestRecipient(e.target.value)}
                  placeholder="you@example.com"
                />
              </FormField>
              <Button
                type="button"
                variant="outline"
                disabled={test.isPending || isLoading}
                onClick={() => test.mutate()}
                className="mb-[1px]"
              >
                {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                Send test
              </Button>
            </div>
            <Button type="submit" disabled={save.isPending || isLoading}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
