import { FormEvent, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Download, KeyRound, Loader2, Mail, Palette, Plug, Save, Send, Settings, User as UserIcon } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useTheme } from '@/stores/theme'
import { useAuth } from '@/stores/auth'
import { toast } from '@/components/ui/Toast'
import { UpdatesTabContent } from '@/components/UpdatesTabContent'
import { LicensesTabContent } from '@/components/LicensesTabContent'
import { SensorsCard } from '@/components/SensorsCard'

const TABS = [
  { value: 'company', label: 'Company', icon: Building2 },
  { value: 'smtp', label: 'SMTP / Email', icon: Mail },
  { value: 'appearance', label: 'Appearance', icon: Palette },
  { value: 'licenses', label: 'Licenses', icon: KeyRound },
  { value: 'updates', label: 'Updates', icon: Download },
  { value: 'sensors', label: 'Sensors', icon: Plug },
  { value: 'profile', label: 'Profile', icon: UserIcon },
] as const

type TabValue = typeof TABS[number]['value']

export function GeneralSettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.get('tab') as TabValue | null
  const active: TabValue = TABS.some((t) => t.value === requested) ? (requested as TabValue) : 'company'

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
          Company information, email, appearance, licenses, updates, sensors, and your profile
        </p>
      </div>

      <Tabs value={active} onValueChange={setActive}>
        <TabsList className="h-auto flex-wrap gap-1 bg-surface2/50 p-1">
          {TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="gap-1.5">
              <t.icon className="h-3.5 w-3.5" />
              <span>{t.label}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="company">
          <CompanyCard />
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
        <TabsContent value="sensors">
          <SensorsCard />
        </TabsContent>
        <TabsContent value="profile">
          <ProfileCard />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function CompanyCard() {
  const qc = useQueryClient()
  const { data } = useQuery<any>({
    queryKey: ['settings', 'company'],
    queryFn: async () => (await api.get('/settings/company')).data,
  })
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (data) setForm(data) }, [data])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/company', form)).data,
    onSuccess: () => { toast.success('Company settings saved'); qc.invalidateQueries({ queryKey: ['settings', 'company'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader><CardTitle>Company</CardTitle></CardHeader>
      <CardContent>
        <form onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate() }} className="grid grid-cols-2 gap-3">
          <FormField label="Company name" className="col-span-2">
            <Input value={form.company_name || ''} onChange={(e) => setForm({ ...form, company_name: e.target.value })} placeholder="Acme Corporation" />
          </FormField>
          <FormField label="Email"><Input type="email" value={form.company_email || ''} onChange={(e) => setForm({ ...form, company_email: e.target.value })} /></FormField>
          <FormField label="Phone"><Input value={form.company_phone || ''} onChange={(e) => setForm({ ...form, company_phone: e.target.value })} /></FormField>
          <FormField label="Website"><Input value={form.company_website || ''} onChange={(e) => setForm({ ...form, company_website: e.target.value })} placeholder="https://example.com" /></FormField>
          <FormField label="Timezone">
            <Select value={form.timezone || 'UTC'} onValueChange={(v) => setForm({ ...form, timezone: v })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {['UTC','Asia/Riyadh','Asia/Dubai','Asia/Karachi','Asia/Kolkata','Asia/Shanghai','Asia/Tokyo','Europe/London','Europe/Paris','Europe/Berlin','Europe/Istanbul','America/New_York','America/Chicago','America/Los_Angeles','Australia/Sydney'].map((tz) => (
                  <SelectItem key={tz} value={tz}>{tz}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Address" className="col-span-2">
            <Textarea value={form.company_address || ''} onChange={(e) => setForm({ ...form, company_address: e.target.value })} />
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
  const { user } = useAuth()
  return (
    <Card>
      <CardHeader><CardTitle>Your Profile</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        {[
          ['Username', user?.username || '—'],
          ['Email', user?.email || '—'],
          ['Full name', user?.full_name || '—'],
          ['Role', user?.role || '—'],
        ].map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
            <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
            <span>{v}</span>
          </div>
        ))}
      </CardContent>
    </Card>
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
