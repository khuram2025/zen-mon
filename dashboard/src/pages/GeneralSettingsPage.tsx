import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Save, Settings } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { useTheme } from '@/stores/theme'
import { useAuth } from '@/stores/auth'
import { toast } from '@/components/ui/Toast'

export function GeneralSettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Settings className="h-5 w-5 text-primary" /> General Settings
        </h1>
        <p className="text-xs text-muted">Company information, appearance, and profile</p>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <CompanyCard />
        <div className="space-y-6">
          <AppearanceCard />
          <ProfileCard />
        </div>
      </div>
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
