import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Mail, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'

export function GatewaysPage() {
  const { data } = useQuery<any>({
    queryKey: ['settings', 'gateways'],
    queryFn: async () => (await api.get('/settings/gateways')).data,
  })

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Mail className="h-5 w-5 text-primary" /> Gateways
        </h1>
        <p className="text-xs text-muted">SMTP and SMS gateway configuration</p>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SmtpCard initial={data?.smtp} />
        <SmsCard initial={data?.sms} />
      </div>
    </div>
  )
}

function SmtpCard({ initial }: { initial?: any }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (initial) setForm(initial) }, [initial])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/gateways/smtp', form)).data,
    onSuccess: () => { toast.success('SMTP gateway saved'); qc.invalidateQueries({ queryKey: ['settings', 'gateways'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const test = useMutation({
    mutationFn: async () => (await api.post('/settings/gateways/smtp/test')).data,
    onSuccess: () => toast.success('Test email sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader><CardTitle>SMTP Gateway</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <FormField label="Host"><Input value={form.host || ''} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="smtp.example.com" /></FormField>
        <div className="grid grid-cols-2 gap-3">
          <FormField label="Port"><Input type="number" value={form.port || ''} onChange={(e) => setForm({ ...form, port: Number(e.target.value) || '' })} placeholder="587" /></FormField>
          <FormField label="TLS">
            <Select value={form.use_tls ? 'tls' : form.use_ssl ? 'ssl' : 'none'} onValueChange={(v) => setForm({ ...form, use_tls: v === 'tls', use_ssl: v === 'ssl' })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="tls">STARTTLS</SelectItem><SelectItem value="ssl">SSL</SelectItem><SelectItem value="none">None</SelectItem></SelectContent>
            </Select>
          </FormField>
        </div>
        <FormField label="Username"><Input value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} /></FormField>
        <FormField label="Password"><Input type="password" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder={initial?.has_password ? '••••••••' : ''} /></FormField>
        <FormField label="From address"><Input value={form.from_email || ''} onChange={(e) => setForm({ ...form, from_email: e.target.value })} placeholder="alerts@example.com" /></FormField>
        <div className="flex gap-2 pt-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save</Button>
          <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>Send test</Button>
        </div>
      </CardContent>
    </Card>
  )
}

function SmsCard({ initial }: { initial?: any }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<any>({})
  useEffect(() => { if (initial) setForm(initial) }, [initial])

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/gateways/sms', form)).data,
    onSuccess: () => { toast.success('SMS gateway saved'); qc.invalidateQueries({ queryKey: ['settings', 'gateways'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const test = useMutation({
    mutationFn: async () => (await api.post('/settings/gateways/sms/test')).data,
    onSuccess: () => toast.success('Test SMS sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader><CardTitle>SMS Gateway</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <FormField label="Provider">
          <Select value={form.provider || 'twilio'} onValueChange={(v) => setForm({ ...form, provider: v })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="twilio">Twilio</SelectItem><SelectItem value="vonage">Vonage</SelectItem><SelectItem value="http">Generic HTTP</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField label="Account SID / API key"><Input value={form.account_sid || ''} onChange={(e) => setForm({ ...form, account_sid: e.target.value })} /></FormField>
        <FormField label="Auth token / secret"><Input type="password" value={form.auth_token || ''} onChange={(e) => setForm({ ...form, auth_token: e.target.value })} placeholder={initial?.has_auth ? '••••••••' : ''} /></FormField>
        <FormField label="From number"><Input value={form.from_number || ''} onChange={(e) => setForm({ ...form, from_number: e.target.value })} placeholder="+15551234567" /></FormField>
        <div className="flex gap-2 pt-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save</Button>
          <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>Send test</Button>
        </div>
      </CardContent>
    </Card>
  )
}
