import { FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Edit3, KeyRound, Loader2, Plus, ShieldCheck, Trash2, X, Zap,
} from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage } from '@/lib/utils'

type WinCred = {
  id: string
  name: string
  username: string
  domain: string | null
  auth_method: 'basic' | 'ntlm' | 'kerberos' | 'credssp' | 'certificate'
  transport: 'http' | 'https'
  port: number
  ssl_verify: boolean
  dc_host: string | null
  description: string | null
  created_at: string
  updated_at: string
}

const DEFAULT_FORM = {
  name: '',
  username: '',
  domain: '',
  password: '',
  auth_method: 'ntlm' as WinCred['auth_method'],
  transport: 'http' as WinCred['transport'],
  port: 5985,
  ssl_verify: false,
  dc_host: '',
  description: '',
}

export function WindowsCredentialsPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<WinCred | null>(null)
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ ...DEFAULT_FORM })
  const [testIp, setTestIp] = useState('')
  const [testResult, setTestResult] = useState<any>(null)

  const { data: creds = [], isLoading } = useQuery<WinCred[]>({
    queryKey: ['windows-credentials'],
    queryFn: async () => (await api.get('/windows-credentials')).data,
  })

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload: any = { ...form }
      if (!payload.domain) delete payload.domain
      if (!payload.description) delete payload.description
      if (editing) {
        if (!payload.password) delete payload.password
        return (await api.patch(`/windows-credentials/${editing.id}`, payload)).data
      }
      return (await api.post('/windows-credentials', payload)).data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['windows-credentials'] })
      toast.success(editing ? 'Credential updated' : 'Credential created')
      setOpen(false)
      setEditing(null)
      setForm({ ...DEFAULT_FORM })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/windows-credentials/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['windows-credentials'] })
      toast.success('Credential deleted')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const testMutation = useMutation({
    mutationFn: async ({ id, ip }: { id: string; ip: string }) =>
      (await api.post(`/windows-credentials/${id}/test`, { ip })).data,
    onSuccess: (r) => {
      setTestResult(r)
      if (r.ok) toast.success('Credential verified', 'WinRM session and Security log access both OK')
      else {
        const failed = (r.checks || []).find((c: any) => !c.ok)
        toast.error(failed ? `${failed.label} failed` : 'Test failed', r.error || r.state || 'unknown')
      }
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  function openCreate() {
    setEditing(null)
    setForm({ ...DEFAULT_FORM })
    setOpen(true)
  }
  function openEdit(c: WinCred) {
    setEditing(c)
    setForm({
      name: c.name,
      username: c.username,
      domain: c.domain || '',
      password: '',
      auth_method: c.auth_method,
      transport: c.transport,
      port: c.port,
      ssl_verify: c.ssl_verify,
      dc_host: c.dc_host || '',
      description: c.description || '',
    })
    setOpen(true)
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    saveMutation.mutate()
  }

  return (
    <div className="space-y-4">
      {hideHeader ? (
        <div className="flex items-center justify-end">
          <Button onClick={openCreate}>
            <Plus className="h-4 w-4" /> New credential
          </Button>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <ShieldCheck className="h-6 w-6 text-primary" /> Windows Credentials
            </h1>
            <p className="mt-1 text-xs text-muted">
              WMI / WinRM credentials used by discovery to inventory Windows hosts.
              Passwords are encrypted at rest.
            </p>
          </div>
          <Button onClick={openCreate}>
            <Plus className="h-4 w-4" /> New credential
          </Button>
        </div>
      )}

      {isLoading ? (
        <Card><CardContent className="py-12 text-center text-sm text-muted">Loading…</CardContent></Card>
      ) : creds.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
              <KeyRound className="h-7 w-7 text-primary" />
            </div>
            <h3 className="text-lg font-semibold">No Windows credentials yet</h3>
            <p className="max-w-md text-sm text-muted">
              Add credentials so discovery can identify Windows hosts via WinRM (or WMI).
              Typically a domain service account with WMI / WinRM permissions.
            </p>
            <Button onClick={openCreate}>
              <Plus className="h-4 w-4" /> Add first credential
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Name</Th>
                  <Th>Username</Th>
                  <Th>Domain controller</Th>
                  <Th>Auth</Th>
                  <Th>Transport</Th>
                  <Th>Port</Th>
                  <Th className="w-44 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {creds.map((c) => (
                  <Tr key={c.id}>
                    <Td>
                      <div className="font-medium">{c.name}</div>
                      {c.description && <div className="text-xs text-muted">{c.description}</div>}
                    </Td>
                    <Td className="font-mono text-xs">
                      {c.domain ? `${c.domain}\\${c.username}` : c.username}
                    </Td>
                    <Td className="font-mono text-xs">
                      {c.dc_host || <span className="font-sans text-muted">Not set</span>}
                    </Td>
                    <Td><Badge variant="outline">{c.auth_method}</Badge></Td>
                    <Td>
                      <Badge variant={c.transport === 'https' ? 'success' : 'default'}>
                        {c.transport.toUpperCase()}
                      </Badge>
                    </Td>
                    <Td className="font-mono text-xs">{c.port}</Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="icon" onClick={() => openEdit(c)}
                                className="h-8 w-8" title="Edit">
                          <Edit3 className="h-4 w-4" />
                        </Button>
                        <Button variant="ghost" size="icon"
                                onClick={() => {
                                  setEditing(c)
                                  setTestIp(c.dc_host || '')
                                  setTestResult(null)
                                }}
                                className="h-8 w-8" title="Test the connection to the domain controller">
                          <Zap className="h-4 w-4" />
                        </Button>
                        <Button variant="ghost" size="icon"
                                className="h-8 w-8 text-muted hover:text-danger"
                                onClick={() => {
                                  if (confirm(`Delete credential "${c.name}"?`))
                                    deleteMutation.mutate(c.id)
                                }} title="Delete">
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* Create / edit dialog */}
      <Dialog open={open} onOpenChange={(o) => !o && (setOpen(false), setEditing(null))}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? 'Edit credential' : 'New Windows credential'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={onSubmit} className="space-y-3">
            <FormField label="Name" required>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                     placeholder="windows-admin" required />
            </FormField>
            <div className="grid gap-3 md:grid-cols-2">
              <FormField label="Username" required>
                <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })}
                       placeholder="discovery-svc" required />
              </FormField>
              <FormField label="Domain (optional)">
                <Input value={form.domain} onChange={(e) => setForm({ ...form, domain: e.target.value })}
                       placeholder="CORP" />
              </FormField>
            </div>
            <FormField label={editing ? 'Password (leave blank to keep)' : 'Password'} required={!editing}>
              <PasswordInput value={form.password}
                             onChange={(e) => setForm({ ...form, password: e.target.value })}
                             placeholder="••••••••" required={!editing} />
            </FormField>
            <div className="grid gap-3 md:grid-cols-3">
              <FormField label="Auth method">
                <Select value={form.auth_method}
                        onValueChange={(v) => setForm({ ...form, auth_method: v as any })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ntlm">NTLM</SelectItem>
                    <SelectItem value="kerberos">Kerberos</SelectItem>
                    <SelectItem value="basic">Basic</SelectItem>
                    <SelectItem value="credssp">CredSSP</SelectItem>
                    <SelectItem value="certificate">Certificate</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Transport">
                <Select value={form.transport}
                        onValueChange={(v) => setForm({
                          ...form,
                          transport: v as any,
                          port: v === 'https' ? 5986 : 5985,
                        })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="http">HTTP (5985)</SelectItem>
                    <SelectItem value="https">HTTPS (5986)</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Port">
                <Input type="number" min={1} max={65535} value={form.port}
                       onChange={(e) => setForm({ ...form, port: Number(e.target.value) || 5985 })} />
              </FormField>
            </div>
            {form.transport === 'https' && (
              <FormField label="Verify TLS certificate">
                <div className="flex items-center gap-2 rounded-md border border-border px-3 py-2">
                  <Switch checked={form.ssl_verify}
                          onCheckedChange={(v) => setForm({ ...form, ssl_verify: v })} />
                  <span className="text-sm">{form.ssl_verify ? 'Enabled' : 'Disabled (recommended for self-signed)'}</span>
                </div>
              </FormField>
            )}
            <FormField label="Domain controller (optional)"
                       hint="Hostname or IP this credential is used against. Sets the target for the connection test, and is required before UDT can correlate user logins.">
              <Input value={form.dc_host}
                     onChange={(e) => setForm({ ...form, dc_host: e.target.value })}
                     placeholder="dc01.corp.local" />
            </FormField>
            <FormField label="Description (optional)">
              <Input value={form.description}
                     onChange={(e) => setForm({ ...form, description: e.target.value })}
                     placeholder="What this credential is used for" />
            </FormField>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Test dialog */}
      <Dialog open={!!editing && !open}
              onOpenChange={(o) => !o && (setEditing(null), setTestResult(null))}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Test {editing?.name}</DialogTitle>
          </DialogHeader>
          <FormField label="Domain controller"
                     hint={editing?.dc_host
                       ? 'Checks the two rights UDT needs: a WinRM session, and read access to the Security event log.'
                       : 'This credential has no domain controller set \u2014 add one on the credential first. Testing a different host is an administrator action.'}>
            <Input value={testIp} onChange={(e) => setTestIp(e.target.value)}
                   placeholder="dc01.corp.local" />
          </FormField>
          {testResult && (
            <div className="space-y-2">
              {/* One row per access check. WinRM session and Security log access
                  come from different groups, so a single verdict would send the
                  operator to the wrong one. */}
              <div className="divide-y divide-border rounded-md border border-border">
                {(testResult.checks || []).map((c: any) => (
                  <div key={c.id} className="flex items-start gap-2 px-3 py-2 text-xs">
                    <span className={c.ok ? 'text-success' : 'text-danger'}>{c.ok ? '✓' : '✗'}</span>
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-text">{c.label}</div>
                      {c.detail && <div className="mt-0.5 break-words text-muted">{c.detail}</div>}
                      {!c.ok && c.fix && (
                        <div className="mt-1 rounded border border-warning/30 bg-warning/10 px-2 py-1 text-warning">
                          {c.fix}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
              {testResult.info?.hostname && (
                <div className="text-xs text-muted">
                  Reached <span className="font-mono text-text">{testResult.info.hostname}</span>
                  {testResult.info.os ? ` · ${testResult.info.os}` : ''}
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => { setEditing(null); setTestResult(null) }}>
              Close
            </Button>
            <Button
              disabled={!testIp || testMutation.isPending}
              onClick={() => editing && testMutation.mutate({ id: editing.id, ip: testIp.trim() })}>
              {testMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              <Zap className="h-4 w-4" /> Run test
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
