import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Loader2, Network, Plus, Save, Server, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { Role } from '@/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'

type Mapping = { group?: string; value?: string; role: string }

type LdapConfig = {
  enabled: boolean; server: string; port: number; use_ssl: boolean; use_starttls: boolean
  bind_dn: string; bind_password: string; has_bind_password?: boolean
  base_dn: string; user_filter: string; email_attr: string; name_attr: string; group_attr: string
  group_mappings: Mapping[]; default_role: string; auto_provision: boolean
}

type RadiusConfig = {
  enabled: boolean; server: string; port: number; secret: string; has_secret?: boolean
  timeout: number; retries: number; nas_identifier: string
  class_mappings: Mapping[]; default_role: string; auto_provision: boolean
}

type TestResult = {
  success: boolean; message: string
  groups?: string[]; mapped_role?: string | null; would_sign_in?: boolean; reply_values?: string[]
}

/** LDAP / RADIUS provider configuration. Local accounts always keep
 * working — external providers only add sign-in paths. */
export function AuthProvidersSection() {
  const { data, isLoading } = useQuery<{ ldap: LdapConfig; radius: RadiusConfig }>({
    queryKey: ['auth', 'providers'],
    queryFn: async () => (await api.get('/system/auth/providers')).data,
  })
  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })

  if (isLoading || !data) {
    return <div className="py-10 text-center text-sm text-muted"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></div>
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        Users signing in with an unknown username are checked against enabled providers (LDAP first, then RADIUS)
        and provisioned automatically with the mapped role. Local accounts always authenticate locally.
      </p>
      <LdapCard initial={data.ldap} roles={roles || []} />
      <RadiusCard initial={data.radius} roles={roles || []} />
    </div>
  )
}

function RoleSelect({ value, onChange, roles, allowNone, noneLabel }: {
  value: string; onChange: (v: string) => void; roles: Role[]; allowNone?: boolean; noneLabel?: string
}) {
  return (
    <Select value={value || (allowNone ? '__none__' : value)} onValueChange={(v) => onChange(v === '__none__' ? '' : v)}>
      <SelectTrigger><SelectValue /></SelectTrigger>
      <SelectContent>
        {allowNone && <SelectItem value="__none__">{noneLabel || '— none (deny) —'}</SelectItem>}
        {roles.map((r) => <SelectItem key={r.name} value={r.name}>{r.display_name}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

function MappingRows({ mappings, setMappings, keyField, keyLabel, keyPlaceholder, roles }: {
  mappings: Mapping[]
  setMappings: (m: Mapping[]) => void
  keyField: 'group' | 'value'
  keyLabel: string
  keyPlaceholder: string
  roles: Role[]
}) {
  return (
    <div className="space-y-2">
      {mappings.map((m, i) => (
        <div key={i} className="flex items-center gap-2">
          <Input
            className="flex-1"
            placeholder={keyPlaceholder}
            value={(m as any)[keyField] || ''}
            onChange={(e) => setMappings(mappings.map((x, j) => (j === i ? { ...x, [keyField]: e.target.value } : x)))}
          />
          <div className="w-44">
            <RoleSelect
              value={m.role}
              roles={roles}
              onChange={(v) => setMappings(mappings.map((x, j) => (j === i ? { ...x, role: v } : x)))}
            />
          </div>
          <Button type="button" variant="ghost" size="icon" className="h-8 w-8 text-muted hover:text-danger"
            onClick={() => setMappings(mappings.filter((_, j) => j !== i))}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      <Button type="button" variant="outline" size="sm"
        onClick={() => setMappings([...mappings, { [keyField]: '', role: roles[0]?.name || 'viewer' } as Mapping])}>
        <Plus className="h-3.5 w-3.5" /> Add {keyLabel}
      </Button>
    </div>
  )
}

function TestPanel({ endpoint, requireCreds, resultExtra }: {
  endpoint: string
  requireCreds?: boolean
  resultExtra?: (r: TestResult) => React.ReactNode
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [result, setResult] = useState<TestResult | null>(null)

  const test = useMutation({
    mutationFn: async () => (await api.post(endpoint, { username, password })).data as TestResult,
    onSuccess: (r) => setResult(r),
    onError: (e: any) => setResult({ success: false, message: apiErrorMessage(e) }),
  })

  return (
    <div className="rounded-md border border-border bg-surface2/30 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted">
        <FlaskConical className="h-3.5 w-3.5" /> Test (uses saved settings)
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <FormField label="Username" className="w-44">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder={requireCreds ? 'testuser' : 'optional'} autoComplete="off" />
        </FormField>
        <FormField label="Password" className="w-44">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </FormField>
        <Button type="button" variant="outline" disabled={test.isPending || (requireCreds && (!username || !password))} onClick={() => test.mutate()}>
          {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />} Run test
        </Button>
      </div>
      {result && (
        <div className={`mt-2 rounded-md border p-2 text-xs ${result.success ? 'border-success/30 bg-success/10 text-success' : 'border-danger/30 bg-danger/10 text-danger'}`}>
          <div className="font-medium">{result.message}</div>
          {resultExtra?.(result)}
        </div>
      )}
    </div>
  )
}

function LdapCard({ initial, roles }: { initial: LdapConfig; roles: Role[] }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<LdapConfig>(initial)
  useEffect(() => setForm(initial), [initial])
  const f = (patch: Partial<LdapConfig>) => setForm((prev) => ({ ...prev, ...patch }))

  const save = useMutation({
    mutationFn: async () => (await api.put('/system/auth/ldap', form)).data,
    onSuccess: () => { toast.success('LDAP settings saved'); qc.invalidateQueries({ queryKey: ['auth', 'providers'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Server className="h-4 w-4 text-primary" /> LDAP / Active Directory
          {form.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>}
        </CardTitle>
        <p className="text-xs text-muted">
          Authenticates via a service-account search followed by a bind as the user. Group membership maps to a role on every sign-in.
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-3">
          <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
            <div>
              <p className="text-sm font-medium">Enable LDAP authentication</p>
              <p className="text-[11px] text-muted">Unknown usernames will be looked up in the directory at sign-in.</p>
            </div>
            <Switch checked={form.enabled} onCheckedChange={(v) => f({ enabled: !!v })} />
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <FormField label="Server" className="md:col-span-2">
              <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="dc01.corp.local" />
            </FormField>
            <FormField label="Port">
              <Input type="number" value={form.port} onChange={(e) => f({ port: Number(e.target.value) || 389 })} />
            </FormField>
            <FormField label="Encryption">
              <Select
                value={form.use_ssl ? 'ldaps' : form.use_starttls ? 'starttls' : 'none'}
                onValueChange={(v) => f({ use_ssl: v === 'ldaps', use_starttls: v === 'starttls', port: v === 'ldaps' ? 636 : 389 })}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None (389)</SelectItem>
                  <SelectItem value="starttls">StartTLS (389)</SelectItem>
                  <SelectItem value="ldaps">LDAPS (636)</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Bind DN (service account)">
              <Input value={form.bind_dn} onChange={(e) => f({ bind_dn: e.target.value })} placeholder="CN=svc-zenplus,OU=Service,DC=corp,DC=local" autoComplete="off" />
            </FormField>
            <FormField label="Bind password">
              <Input type="password" value={form.bind_password} onChange={(e) => f({ bind_password: e.target.value })}
                placeholder={form.has_bind_password ? '•••••••• (saved)' : ''} autoComplete="new-password" />
            </FormField>
            <FormField label="Base DN" className="md:col-span-2">
              <Input value={form.base_dn} onChange={(e) => f({ base_dn: e.target.value })} placeholder="DC=corp,DC=local" />
            </FormField>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="User filter" hint="{username} is replaced with the login name">
              <Input value={form.user_filter} onChange={(e) => f({ user_filter: e.target.value })} placeholder="(sAMAccountName={username})" />
            </FormField>
            <div className="grid grid-cols-3 gap-2">
              <FormField label="Email attr"><Input value={form.email_attr} onChange={(e) => f({ email_attr: e.target.value })} /></FormField>
              <FormField label="Name attr"><Input value={form.name_attr} onChange={(e) => f({ name_attr: e.target.value })} /></FormField>
              <FormField label="Group attr"><Input value={form.group_attr} onChange={(e) => f({ group_attr: e.target.value })} /></FormField>
            </div>
          </div>

          <div className="rounded-md border border-border p-3">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Group → role mappings (first match wins)</div>
            <MappingRows
              mappings={form.group_mappings}
              setMappings={(m) => f({ group_mappings: m })}
              keyField="group" keyLabel="mapping"
              keyPlaceholder="Group DN or CN, e.g. CN=NetAdmins,OU=Groups,DC=corp,DC=local"
              roles={roles}
            />
            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <FormField label="Default role (no group matched)">
                <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone />
              </FormField>
              <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
                <span className="text-xs font-medium">Auto-create accounts on first sign-in</span>
                <Switch checked={form.auto_provision} onCheckedChange={(v) => f({ auto_provision: !!v })} />
              </div>
            </div>
          </div>

          <TestPanel
            endpoint="/system/auth/ldap/test"
            resultExtra={(r) => r.success && r.groups ? (
              <div className="mt-1 space-y-0.5 text-text">
                <div>Mapped role: <span className="font-semibold">{r.mapped_role || 'none — sign-in would be denied'}</span></div>
                <div className="text-muted">Groups: {r.groups.length ? r.groups.join(', ') : '(none)'}</div>
              </div>
            ) : null}
          />

          <div className="flex justify-end">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save LDAP settings
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function RadiusCard({ initial, roles }: { initial: RadiusConfig; roles: Role[] }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<RadiusConfig>(initial)
  useEffect(() => setForm(initial), [initial])
  const f = (patch: Partial<RadiusConfig>) => setForm((prev) => ({ ...prev, ...patch }))

  const save = useMutation({
    mutationFn: async () => (await api.put('/system/auth/radius', form)).data,
    onSuccess: () => { toast.success('RADIUS settings saved'); qc.invalidateQueries({ queryKey: ['auth', 'providers'] }) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Network className="h-4 w-4 text-primary" /> RADIUS
          {form.enabled ? <Badge variant="success">Enabled</Badge> : <Badge variant="outline">Disabled</Badge>}
        </CardTitle>
        <p className="text-xs text-muted">
          PAP authentication against a RADIUS server. The Class or Filter-Id reply attribute can map users to roles.
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-3">
          <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
            <div>
              <p className="text-sm font-medium">Enable RADIUS authentication</p>
              <p className="text-[11px] text-muted">Tried after LDAP for unknown usernames.</p>
            </div>
            <Switch checked={form.enabled} onCheckedChange={(v) => f({ enabled: !!v })} />
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
            <FormField label="Server" className="col-span-2">
              <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="radius.corp.local" />
            </FormField>
            <FormField label="Port">
              <Input type="number" value={form.port} onChange={(e) => f({ port: Number(e.target.value) || 1812 })} />
            </FormField>
            <FormField label="Shared secret">
              <Input type="password" value={form.secret} onChange={(e) => f({ secret: e.target.value })}
                placeholder={form.has_secret ? '•••••••• (saved)' : ''} autoComplete="new-password" />
            </FormField>
            <FormField label="Timeout (s)">
              <Input type="number" value={form.timeout} onChange={(e) => f({ timeout: Number(e.target.value) || 5 })} />
            </FormField>
            <FormField label="Retries">
              <Input type="number" value={form.retries} onChange={(e) => f({ retries: Number(e.target.value) || 3 })} />
            </FormField>
          </div>

          <div className="rounded-md border border-border p-3">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Reply attribute → role mappings (first match wins)</div>
            <MappingRows
              mappings={form.class_mappings}
              setMappings={(m) => f({ class_mappings: m })}
              keyField="value" keyLabel="mapping"
              keyPlaceholder="Class / Filter-Id value, e.g. netadmins"
              roles={roles}
            />
            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <FormField label="Default role (no attribute matched)">
                <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone />
              </FormField>
              <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
                <span className="text-xs font-medium">Auto-create accounts on first sign-in</span>
                <Switch checked={form.auto_provision} onCheckedChange={(v) => f({ auto_provision: !!v })} />
              </div>
            </div>
          </div>

          <TestPanel
            endpoint="/system/auth/radius/test"
            requireCreds
            resultExtra={(r) => r.success ? (
              <div className="mt-1 space-y-0.5 text-text">
                <div>Mapped role: <span className="font-semibold">{r.mapped_role || 'none — sign-in would be denied'}</span></div>
                {!!r.reply_values?.length && <div className="text-muted">Reply attributes: {r.reply_values.join(', ')}</div>}
              </div>
            ) : null}
          />

          <div className="flex justify-end">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save RADIUS settings
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
