import { FormEvent, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Edit3, ExternalLink, Globe2, KeyRound, Loader2, LockKeyhole, Plus, Search, ShieldCheck, Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useAuth, hasPermission } from '@/stores/auth'
import type { ServiceCredential } from '@/types'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'

type AuthType = ServiceCredential['auth_type']

type CredentialForm = {
  name: string
  auth_type: AuthType
  username: string
  secret: string
  description: string
}

const EMPTY_FORM: CredentialForm = {
  name: '',
  auth_type: 'ntlm',
  username: '',
  secret: '',
  description: '',
}

const AUTH_LABELS: Record<AuthType, string> = {
  ntlm: 'Windows Integrated (NTLM)',
  basic: 'HTTP Basic',
  bearer: 'Bearer token',
  form: 'Form login',
}

const AUTH_DESCRIPTIONS: Record<AuthType, string> = {
  ntlm: 'For IIS and other services that advertise Negotiate or NTLM.',
  basic: 'Sends a username and password using the HTTP Basic challenge.',
  bearer: 'Sends the saved token in the Authorization header.',
  form: 'Injects credentials into an explicit multi-step sign-in journey.',
}

function updatedLabel(value: string | null) {
  if (!value) return 'Not updated'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Not updated' : date.toLocaleString()
}

export function ServiceCredentialsPage() {
  const qc = useQueryClient()
  const user = useAuth((state) => state.user)
  const canManage = user?.role === 'owner' || hasPermission(user, 'system.admin')
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<ServiceCredential | null>(null)
  const [form, setForm] = useState<CredentialForm>({ ...EMPTY_FORM })

  const { data: credentials = [], isLoading } = useQuery<ServiceCredential[]>({
    queryKey: ['service-credentials'],
    queryFn: async () => (await api.get('/service-credentials')).data,
  })

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return credentials
    return credentials.filter((credential) =>
      [credential.name, credential.username, credential.description, AUTH_LABELS[credential.auth_type]]
        .some((value) => value?.toLowerCase().includes(needle)),
    )
  }, [credentials, query])

  const usageCount = credentials.reduce((sum, credential) => sum + credential.used_by, 0)
  const ntlmCount = credentials.filter((credential) => credential.auth_type === 'ntlm').length

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload: Record<string, string> = {
        name: form.name.trim(),
        auth_type: form.auth_type,
        description: form.description.trim(),
      }
      payload.username = form.auth_type === 'bearer' ? '' : form.username.trim()
      if (!editing || form.secret) payload.secret = form.secret
      if (editing) return (await api.put(`/service-credentials/${editing.id}`, payload)).data
      return (await api.post('/service-credentials', payload)).data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['service-credentials'] })
      toast.success(editing ? 'Service credential updated' : 'Service credential created')
      closeDialog()
    },
    onError: (error: any) => toast.error('Credential save failed', apiErrorMessage(error)),
  })

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-credentials/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['service-credentials'] })
      toast.success('Service credential deleted')
    },
    onError: (error: any) => toast.error('Credential delete failed', apiErrorMessage(error)),
  })

  function closeDialog() {
    setOpen(false)
    setEditing(null)
    setForm({ ...EMPTY_FORM })
  }

  function openCreate() {
    setEditing(null)
    setForm({ ...EMPTY_FORM })
    setOpen(true)
  }

  function openEdit(credential: ServiceCredential) {
    setEditing(credential)
    setForm({
      name: credential.name,
      auth_type: credential.auth_type,
      username: credential.username || '',
      secret: '',
      description: credential.description || '',
    })
    setOpen(true)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    saveMutation.mutate()
  }

  function remove(credential: ServiceCredential) {
    if (credential.used_by > 0) return
    if (window.confirm(`Delete service credential "${credential.name}"? This cannot be undone.`)) {
      deleteMutation.mutate(credential.id)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Card><CardContent className="flex items-center gap-3 p-4">
          <div className="rounded-lg bg-primary/10 p-2 text-primary"><KeyRound className="h-5 w-5" /></div>
          <div><div className="text-xl font-semibold">{credentials.length}</div><div className="text-xs text-muted">Saved credentials</div></div>
        </CardContent></Card>
        <Card><CardContent className="flex items-center gap-3 p-4">
          <div className="rounded-lg bg-success/10 p-2 text-success"><ShieldCheck className="h-5 w-5" /></div>
          <div><div className="text-xl font-semibold">{usageCount}</div><div className="text-xs text-muted">Linked service checks</div></div>
        </CardContent></Card>
        <Card><CardContent className="flex items-center gap-3 p-4">
          <div className="rounded-lg bg-info/10 p-2 text-info"><Globe2 className="h-5 w-5" /></div>
          <div><div className="text-xl font-semibold">{ntlmCount}</div><div className="text-xs text-muted">Windows Integrated</div></div>
        </CardContent></Card>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold">URL and service credentials</h2>
          <p className="text-xs text-muted">Encrypted credentials used by HTTP checks and authenticated service journeys.</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} className="w-56 pl-8" placeholder="Search credentials…" />
          </div>
          <Button onClick={openCreate} disabled={!canManage} title={canManage ? 'Add service credential' : 'Administrator permission is required'}>
            <Plus className="h-4 w-4" /> New credential
          </Button>
        </div>
      </div>

      {isLoading ? (
        <Card><CardContent className="flex items-center justify-center gap-2 py-14 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading credentials…</CardContent></Card>
      ) : credentials.length === 0 ? (
        <Card><CardContent className="flex flex-col items-center gap-3 py-16 text-center">
          <div className="rounded-full bg-primary/10 p-4 text-primary"><LockKeyhole className="h-7 w-7" /></div>
          <div><h3 className="font-semibold">No service credentials yet</h3><p className="mt-1 max-w-lg text-sm text-muted">Add an encrypted credential here, then select it from any HTTP service check.</p></div>
          <Button onClick={openCreate} disabled={!canManage}><Plus className="h-4 w-4" /> Add first credential</Button>
        </CardContent></Card>
      ) : (
        <Card><CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50"><Tr>
              <Th>Name</Th><Th>Authentication</Th><Th>Identity</Th><Th>Usage</Th><Th>Last updated</Th><Th className="w-32 text-right">Actions</Th>
            </Tr></THead>
            <TBody>
              {filtered.map((credential) => (
                <Tr key={credential.id}>
                  <Td><div className="font-medium">{credential.name}</div>{credential.description && <div className="max-w-xs truncate text-xs text-muted">{credential.description}</div>}</Td>
                  <Td><Badge variant={credential.auth_type === 'ntlm' ? 'info' : 'outline'}>{AUTH_LABELS[credential.auth_type]}</Badge></Td>
                  <Td className="font-mono text-xs">{credential.username || <span className="font-sans text-muted">Token only</span>}</Td>
                  <Td>{credential.used_by > 0 ? <Badge variant="success">{credential.used_by} service{credential.used_by === 1 ? '' : 's'}</Badge> : <Badge variant="outline">Unused</Badge>}</Td>
                  <Td className="text-xs text-muted">{updatedLabel(credential.updated_at || credential.created_at)}</Td>
                  <Td><div className="flex items-center justify-end gap-1">
                    <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(credential)} disabled={!canManage} title="Edit credential"><Edit3 className="h-4 w-4" /></Button>
                    <Button variant="ghost" size="icon" className="h-8 w-8 text-muted hover:text-danger" onClick={() => remove(credential)} disabled={!canManage || credential.used_by > 0 || deleteMutation.isPending} title={credential.used_by > 0 ? 'Detach this credential from all service checks before deleting it' : 'Delete credential'}><Trash2 className="h-4 w-4" /></Button>
                  </div></Td>
                </Tr>
              ))}
              {filtered.length === 0 && <Tr><Td colSpan={6} className="py-12 text-center text-sm text-muted">No credentials match your search.</Td></Tr>}
            </TBody>
          </Table>
        </CardContent></Card>
      )}

      <div className="flex items-center justify-between rounded-lg border border-border bg-surface2/30 px-4 py-3 text-xs text-muted">
        <span>Secrets are encrypted at rest and are never returned to the browser after saving.</span>
        <Button asChild variant="outline" size="sm"><Link to="/services">Open services <ExternalLink className="h-3.5 w-3.5" /></Link></Button>
      </div>

      <Dialog open={open} onOpenChange={(value) => { if (!value) closeDialog() }}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>{editing ? 'Edit URL / service credential' : 'New URL / service credential'}</DialogTitle></DialogHeader>
          <form onSubmit={submit} className="space-y-4">
            <FormField label="Name" required><Input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Production IIS account" required /></FormField>
            <FormField label="Authentication type">
              <Select value={form.auth_type} onValueChange={(value) => setForm({ ...form, auth_type: value as AuthType, username: value === 'bearer' ? '' : form.username })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="ntlm">Windows Integrated (NTLM)</SelectItem>
                  <SelectItem value="basic">HTTP Basic</SelectItem>
                  <SelectItem value="bearer">Bearer token</SelectItem>
                  <SelectItem value="form">Form login journey</SelectItem>
                </SelectContent>
              </Select>
              <p className="mt-1.5 text-[11px] text-muted">{AUTH_DESCRIPTIONS[form.auth_type]}</p>
            </FormField>
            {form.auth_type !== 'bearer' && <FormField label="Username" required><Input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} placeholder={form.auth_type === 'ntlm' ? 'DOMAIN\\service-account' : 'service-account'} required /></FormField>}
            <FormField label={editing ? `${form.auth_type === 'bearer' ? 'Token' : 'Password'} (leave blank to keep current)` : form.auth_type === 'bearer' ? 'Token' : 'Password'} required={!editing}>
              <PasswordInput value={form.secret} onChange={(event) => setForm({ ...form, secret: event.target.value })} placeholder={editing ? 'Leave blank to preserve encrypted secret' : 'Enter secret'} required={!editing} />
            </FormField>
            <FormField label="Description"><Input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="Where and why this credential is used" /></FormField>
            <div className="rounded-md border border-warning/25 bg-warning/5 px-3 py-2 text-[11px] text-warning">
              Prefer HTTPS for Basic, bearer, and form authentication. NTLM over trusted HTTP protects the password exchange, but the page content remains unencrypted.
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeDialog}>Cancel</Button>
              <Button type="submit" disabled={saveMutation.isPending || !form.name.trim() || (!editing && !form.secret) || (form.auth_type !== 'bearer' && !form.username.trim())}>
                {saveMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}{editing ? 'Save changes' : 'Save credential'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
