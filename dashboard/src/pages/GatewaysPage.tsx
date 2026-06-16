import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Loader2, Mail, MessageSquare, Plus, Pencil, Trash2, Send, Star, ShieldCheck, AlertCircle, CheckCircle2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Badge } from '@/components/ui/Badge'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'

type GatewayType = 'smtp' | 'sms'

interface Gateway {
  id: string
  name: string
  type: GatewayType
  config: Record<string, any>
  is_default: boolean
  enabled: boolean
}

const clone = <T,>(o: T): T => JSON.parse(JSON.stringify(o))

const EMPTY_SMTP = {
  type: 'smtp' as GatewayType, name: '', enabled: true, is_default: false,
  config: { host: '', port: 587, encryption: 'tls', username: '', password: '', from_email: '', from_name: 'ZenPlus' },
}
const EMPTY_SMS = {
  type: 'sms' as GatewayType, name: '', enabled: true, is_default: false,
  config: {
    provider: 'custom_http', api_url: '', http_method: 'GET', content_type: '',
    auth_type: 'none', auth_username: '', auth_password: '', auth_token_value: '',
    request_template: '', sender_name: 'ZenPlus', account_sid: '', auth_token: '', from_number: '',
  },
}

/** Older builds stored use_tls/use_ssl; surface them as `encryption` for editing. */
function normalizeForEdit(g: Gateway): any {
  const copy = clone(g)
  if (g.type === 'smtp' && !copy.config.encryption) {
    copy.config.encryption = copy.config.use_ssl ? 'ssl' : copy.config.use_tls ? 'tls' : 'none'
  }
  return copy
}

export function GatewaysPage() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery<{ data: Gateway[] }>({
    queryKey: ['settings', 'gateways', 'list'],
    queryFn: async () => (await api.get('/settings/gateways/list')).data,
  })
  const gateways = data?.data ?? []

  const [editing, setEditing] = useState<any>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [deleting, setDeleting] = useState<Gateway | null>(null)
  const [testing, setTesting] = useState<Gateway | null>(null)

  const invalidate = () => qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/settings/gateways/${id}`),
    onSuccess: () => { toast.success('Gateway deleted'); invalidate(); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const setDefault = useMutation({
    mutationFn: async (g: Gateway) => (await api.put(`/settings/gateways/${g.id}`, { is_default: true })).data,
    onSuccess: () => { toast.success('Default gateway updated'); invalidate() },
    onError: (e: any) => toast.error('Could not set default', apiErrorMessage(e)),
  })
  const toggleEnabled = useMutation({
    mutationFn: async (g: Gateway) => (await api.put(`/settings/gateways/${g.id}`, { enabled: !g.enabled })).data,
    onSuccess: () => invalidate(),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  function openCreate(type: GatewayType) {
    setEditing(clone(type === 'smtp' ? EMPTY_SMTP : EMPTY_SMS))
    setFormOpen(true)
  }
  function openEdit(g: Gateway) {
    setEditing(normalizeForEdit(g))
    setFormOpen(true)
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Mail className="h-5 w-5 text-primary" /> Gateways
        </h1>
        <p className="text-xs text-muted">
          Configure multiple SMTP and SMS gateways. Notification channels send through the gateway you assign them,
          or the default for their type.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <GatewaySection
          type="smtp" title="SMTP Gateways" icon={Mail}
          gateways={gateways.filter((g) => g.type === 'smtp')} loading={isLoading}
          onCreate={() => openCreate('smtp')} onEdit={openEdit}
          onDelete={setDeleting} onTest={setTesting}
          onSetDefault={(g) => setDefault.mutate(g)} onToggle={(g) => toggleEnabled.mutate(g)}
        />
        <GatewaySection
          type="sms" title="SMS Gateways" icon={MessageSquare}
          gateways={gateways.filter((g) => g.type === 'sms')} loading={isLoading}
          onCreate={() => openCreate('sms')} onEdit={openEdit}
          onDelete={setDeleting} onTest={setTesting}
          onSetDefault={(g) => setDefault.mutate(g)} onToggle={(g) => toggleEnabled.mutate(g)}
        />
      </div>

      <GatewayFormDialog open={formOpen} onOpenChange={setFormOpen} initial={editing} />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete gateway"
        description={deleting ? `Delete "${deleting.name}"? Channels using it will fall back to the default gateway.` : ''}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />

      <GatewayTestDialog gateway={testing} onOpenChange={(o) => !o && setTesting(null)} />
    </div>
  )
}

function GatewaySection({
  type, title, icon: Icon, gateways, loading, onCreate, onEdit, onDelete, onTest, onSetDefault, onToggle,
}: {
  type: GatewayType
  title: string
  icon: any
  gateways: Gateway[]
  loading: boolean
  onCreate: () => void
  onEdit: (g: Gateway) => void
  onDelete: (g: Gateway) => void
  onTest: (g: Gateway) => void
  onSetDefault: (g: Gateway) => void
  onToggle: (g: Gateway) => void
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2"><Icon className="h-4 w-4 text-primary" /> {title}</CardTitle>
        <Button size="sm" onClick={onCreate}><Plus className="h-4 w-4" /> Add</Button>
      </CardHeader>
      <CardContent className="space-y-2">
        {loading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
        ) : gateways.length === 0 ? (
          <div className="rounded-md border border-dashed border-border py-8 text-center text-sm text-muted">
            No {type.toUpperCase()} gateways yet.
            <button className="ml-1 text-primary hover:underline" onClick={onCreate}>Add one</button>.
          </div>
        ) : (
          gateways.map((g) => (
            <div key={g.id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface2/40 p-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{g.name}</span>
                  {g.is_default && <Badge variant="success" className="gap-1"><Star className="h-3 w-3" /> Default</Badge>}
                  <Badge variant={g.enabled ? 'outline' : 'danger'}>{g.enabled ? 'Enabled' : 'Disabled'}</Badge>
                </div>
                <div className="truncate text-xs text-muted">{summarize(g)}</div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Switch checked={g.enabled} onCheckedChange={() => onToggle(g)} aria-label="Toggle enabled" />
                <Button size="icon" variant="ghost" title="Send test" onClick={() => onTest(g)}><Send className="h-4 w-4" /></Button>
                <Button
                  size="icon" variant="ghost" title={g.is_default ? 'Already default' : 'Set as default'}
                  disabled={g.is_default} onClick={() => onSetDefault(g)}
                >
                  <Star className={g.is_default ? 'h-4 w-4 fill-success text-success' : 'h-4 w-4'} />
                </Button>
                <Button size="icon" variant="ghost" title="Edit" onClick={() => onEdit(g)}><Pencil className="h-4 w-4" /></Button>
                <Button size="icon" variant="ghost" title="Delete" onClick={() => onDelete(g)}><Trash2 className="h-4 w-4 text-danger" /></Button>
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  )
}

function summarize(g: Gateway): string {
  const c = g.config || {}
  if (g.type === 'smtp') {
    const enc = c.encryption || (c.use_ssl ? 'ssl' : c.use_tls ? 'tls' : 'none')
    return `${c.host || '(no host)'}:${c.port || 587} · ${String(enc).toUpperCase()} · from ${c.from_email || '—'}`
  }
  if (c.provider === 'custom_http') return `Custom HTTP · ${c.http_method || 'GET'} ${c.api_url || '(no URL)'}`
  return `${c.provider || 'custom'} · from ${c.from_number || '—'}`
}

function GatewayFormDialog({ open, onOpenChange, initial }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  initial: any
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState<any>(initial)
  useEffect(() => { if (open) setForm(initial) }, [initial, open])

  const isEdit = !!form?.id
  const type: GatewayType = form?.type

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        name: (form.name || '').trim(),
        type: form.type,
        enabled: !!form.enabled,
        is_default: !!form.is_default,
        config: form.config,
      }
      if (isEdit) return (await api.put(`/settings/gateways/${form.id}`, payload)).data
      return (await api.post('/settings/gateways', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Gateway updated' : 'Gateway created')
      qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const setConfig = (key: string, value: any) =>
    setForm((p: any) => ({ ...p, config: { ...p.config, [key]: value } }))

  if (!form) return null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit' : 'New'} {type === 'smtp' ? 'SMTP' : 'SMS'} gateway</DialogTitle>
        </DialogHeader>
        <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
          <FormField label="Gateway name" required>
            <Input
              required value={form.name || ''}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={type === 'smtp' ? 'e.g. Microsoft 365 SMTP' : 'e.g. Primary SMS gateway'}
            />
          </FormField>

          {type === 'smtp'
            ? <SmtpFields config={form.config} setConfig={setConfig} />
            : <SmsFields config={form.config} setConfig={setConfig} />}

          <div className="flex flex-wrap items-center gap-6 border-t border-border pt-3">
            <label className="flex items-center gap-2 text-sm">
              <Switch checked={!!form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} /> Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Switch checked={!!form.is_default} onCheckedChange={(v) => setForm({ ...form, is_default: v })} />
              Default for {type === 'smtp' ? 'email' : 'SMS'}
            </label>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create gateway'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function SmtpFields({ config: c, setConfig }: { config: any; setConfig: (k: string, v: any) => void }) {
  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_120px]">
        <FormField label="Host"><Input value={c.host || ''} onChange={(e) => setConfig('host', e.target.value)} placeholder="smtp.example.com" /></FormField>
        <FormField label="Port"><Input type="number" value={c.port ?? ''} onChange={(e) => setConfig('port', Number(e.target.value) || '')} placeholder="587" /></FormField>
      </div>
      <FormField label="Encryption" hint="STARTTLS is usually port 587, SSL/TLS port 465.">
        <Select value={c.encryption || 'tls'} onValueChange={(v) => setConfig('encryption', v)}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="tls">STARTTLS</SelectItem>
            <SelectItem value="ssl">SSL/TLS</SelectItem>
            <SelectItem value="none">None</SelectItem>
          </SelectContent>
        </Select>
      </FormField>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FormField label="Username"><Input value={c.username || ''} autoComplete="off" onChange={(e) => setConfig('username', e.target.value)} /></FormField>
        <FormField label="Password"><PasswordInput value={c.password || ''} onChange={(e) => setConfig('password', e.target.value)} /></FormField>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FormField label="From address"><Input value={c.from_email || ''} onChange={(e) => setConfig('from_email', e.target.value)} placeholder="alerts@example.com" /></FormField>
        <FormField label="From name"><Input value={c.from_name || ''} onChange={(e) => setConfig('from_name', e.target.value)} placeholder="ZenPlus" /></FormField>
      </div>
    </>
  )
}

function SmsFields({ config: c, setConfig }: { config: any; setConfig: (k: string, v: any) => void }) {
  const provider = c.provider || 'custom_http'
  return (
    <>
      <FormField label="Provider">
        <Select value={provider} onValueChange={(v) => setConfig('provider', v)}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="custom_http">Custom HTTP API</SelectItem>
            <SelectItem value="twilio">Twilio</SelectItem>
            <SelectItem value="vonage">Vonage</SelectItem>
          </SelectContent>
        </Select>
      </FormField>

      {provider === 'custom_http' ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_120px]">
            <FormField label="API URL"><Input value={c.api_url || ''} onChange={(e) => setConfig('api_url', e.target.value)} placeholder="https://sms.example.com/send" /></FormField>
            <FormField label="Method">
              <Select value={c.http_method || 'GET'} onValueChange={(v) => setConfig('http_method', v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="GET">GET</SelectItem><SelectItem value="POST">POST</SelectItem></SelectContent>
              </Select>
            </FormField>
          </div>
          <FormField label="Content-Type" hint="For POST: application/json, application/x-www-form-urlencoded, or blank.">
            <Input value={c.content_type || ''} onChange={(e) => setConfig('content_type', e.target.value)} placeholder="application/json" />
          </FormField>
          <FormField label="Authentication">
            <Select value={c.auth_type || 'none'} onValueChange={(v) => setConfig('auth_type', v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="basic">Basic auth</SelectItem>
                <SelectItem value="bearer">Bearer token</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {c.auth_type === 'basic' && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="Auth username"><Input value={c.auth_username || ''} onChange={(e) => setConfig('auth_username', e.target.value)} /></FormField>
              <FormField label="Auth password"><PasswordInput value={c.auth_password || ''} onChange={(e) => setConfig('auth_password', e.target.value)} /></FormField>
            </div>
          )}
          {c.auth_type === 'bearer' && (
            <FormField label="Bearer token"><PasswordInput value={c.auth_token_value || ''} onChange={(e) => setConfig('auth_token_value', e.target.value)} /></FormField>
          )}
          <FormField label="Request template" hint="Placeholders: {recipients} {message} {sender}">
            <Textarea
              rows={3} value={c.request_template || ''}
              onChange={(e) => setConfig('request_template', e.target.value)}
              placeholder={'{"to":"{recipients}","text":"{message}","from":"{sender}"}'}
            />
          </FormField>
          <FormField label="Sender name"><Input value={c.sender_name || ''} onChange={(e) => setConfig('sender_name', e.target.value)} placeholder="ZenPlus" /></FormField>
        </>
      ) : (
        <>
          <FormField label="Account SID / API key"><Input value={c.account_sid || ''} onChange={(e) => setConfig('account_sid', e.target.value)} /></FormField>
          <FormField label="Auth token / secret"><PasswordInput value={c.auth_token || ''} onChange={(e) => setConfig('auth_token', e.target.value)} /></FormField>
          <FormField label="From number"><Input value={c.from_number || ''} onChange={(e) => setConfig('from_number', e.target.value)} placeholder="+15551234567" /></FormField>
        </>
      )}
    </>
  )
}

function GatewayTestDialog({ gateway, onOpenChange }: { gateway: Gateway | null; onOpenChange: (o: boolean) => void }) {
  const [recipient, setRecipient] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  useEffect(() => { setRecipient(''); setError(null); setSuccess(null) }, [gateway])

  const isSmtp = gateway?.type === 'smtp'
  const test = useMutation({
    mutationFn: async () => (await api.post(`/settings/gateways/${gateway!.id}/test`, { recipient })).data,
    onMutate: () => { setError(null); setSuccess(null) },
    // Keep the dialog open and surface the full reason inline so a long SMTP/SMS
    // failure (auth rejected, connection refused, TLS error…) is fully readable.
    onSuccess: (d: any) => { setSuccess(d?.message || 'Test message sent successfully.') },
    onError: (e: any) => { setError(apiErrorMessage(e)) },
  })

  return (
    <Dialog open={!!gateway} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-primary" /> Send test {isSmtp ? 'email' : 'SMS'}
          </DialogTitle>
        </DialogHeader>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (recipient.trim()) test.mutate() }}>
          <p className="text-xs text-muted">Using gateway <span className="font-medium text-text">{gateway?.name}</span>.</p>
          <FormField label={isSmtp ? 'Recipient email' : 'Recipient phone number'} required>
            <Input
              required autoFocus value={recipient}
              onChange={(e) => setRecipient(e.target.value)}
              placeholder={isSmtp ? 'you@example.com' : '+15551234567'}
            />
          </FormField>

          {error && (
            <div className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/10 p-2.5 text-xs text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0">
                <div className="font-medium">Test failed</div>
                <div className="mt-0.5 whitespace-pre-wrap break-words text-danger/90">{error}</div>
              </div>
            </div>
          )}
          {success && (
            <div className="flex items-start gap-2 rounded-md border border-success/30 bg-success/10 p-2.5 text-xs text-success">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 break-words">{success}</div>
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
            <Button type="submit" disabled={test.isPending || !recipient.trim()}>
              {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              {error || success ? 'Retry' : 'Send test'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
