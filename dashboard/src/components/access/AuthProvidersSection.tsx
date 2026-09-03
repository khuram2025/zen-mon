import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight, CheckCircle2, ChevronDown, ChevronRight, FlaskConical, HardDrive, Loader2,
  Network, Plus, Server, Settings2, Trash2, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import type { Role, User } from '@/types'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { StatusDot } from '@/components/ui/StatusDot'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'

// Backend: server/app/api/v1/auth_settings.py (/api/v1/system/auth/*)

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

type Providers = { ldap: LdapConfig; radius: RadiusConfig }
type ProviderKind = 'ldap' | 'radius'

type TestResult = {
  success: boolean; message: string
  dn?: string; email?: string | null; full_name?: string | null
  groups?: string[]; mapped_role?: string | null; would_sign_in?: boolean; reply_values?: string[]
}

const LDAP_DEFAULT_PORTS = new Set([389, 636])

function ldapConfigured(c: LdapConfig) { return !!c.server && !!c.base_dn }
function radiusConfigured(c: RadiusConfig) { return !!c.server && !!(c.secret || c.has_secret) }

/** Settings → Users & Access → Authentication.
 *
 * Appliance-style source list: local accounts plus the LDAP/AD and RADIUS
 * providers, each with its status, server, and role-assignment summary.
 * Editing happens in a dialog so the page stays a one-glance overview. */
export function AuthProvidersSection() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery<Providers>({
    queryKey: ['auth', 'providers'],
    queryFn: async () => (await api.get('/system/auth/providers')).data,
  })
  const { data: roles } = useQuery<Role[]>({
    queryKey: ['roles'],
    queryFn: async () => (await api.get('/roles')).data,
  })
  const { data: users } = useQuery<User[]>({
    queryKey: ['users'],
    queryFn: async () => (await api.get('/users')).data,
  })
  const [editing, setEditing] = useState<ProviderKind | null>(null)
  const [editingTab, setEditingTab] = useState<'connection' | 'mapping' | 'test'>('connection')

  const roleLabel = useMemo(() => {
    const m: Record<string, string> = {}
    for (const r of roles || []) m[r.name] = r.display_name
    return m
  }, [roles])

  const counts = useMemo(() => {
    const c = { local: 0, ldap: 0, radius: 0 }
    for (const u of users || []) c[(u.auth_source || 'local') as keyof typeof c] += 1
    return c
  }, [users])

  const toggle = useMutation({
    mutationFn: async ({ kind, enabled }: { kind: ProviderKind; enabled: boolean }) => {
      if (!data) return
      if (kind === 'ldap') return api.put('/system/auth/ldap', { ...data.ldap, bind_password: '', enabled })
      return api.put('/system/auth/radius', { ...data.radius, secret: '', enabled })
    },
    onSuccess: (_r, v) => {
      toast.success(`${v.kind === 'ldap' ? 'LDAP / Active Directory' : 'RADIUS'} ${v.enabled ? 'enabled' : 'disabled'}`)
      qc.invalidateQueries({ queryKey: ['auth', 'providers'] })
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  function open(kind: ProviderKind, tab: 'connection' | 'mapping' | 'test' = 'connection') {
    setEditingTab(tab)
    setEditing(kind)
  }

  function onToggle(kind: ProviderKind, enabled: boolean) {
    if (!data) return
    const configured = kind === 'ldap' ? ldapConfigured(data.ldap) : radiusConfigured(data.radius)
    if (enabled && !configured) {
      toast.info('Configure the server first', 'Enter the connection details, then enable the source.')
      open(kind)
      return
    }
    toggle.mutate({ kind, enabled })
  }

  if (isLoading || !data) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const ldap = data.ldap
  const radius = data.radius
  const externalEnabled = ldap.enabled || radius.enabled

  return (
    <div className="space-y-3">
      {/* Sign-in order strip */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface2/40 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold uppercase tracking-wider text-muted">Sign-in order</span>
          <OrderStep label="Local accounts" active />
          <ArrowRight className="h-3 w-3 text-muted" />
          <OrderStep label="LDAP / AD" active={ldap.enabled} />
          <ArrowRight className="h-3 w-3 text-muted" />
          <OrderStep label="RADIUS" active={radius.enabled} />
        </div>
        <p className="text-[11px] text-muted">
          {externalEnabled
            ? 'Unknown usernames are checked against the enabled external sources in this order and given the mapped role.'
            : 'Only local accounts can sign in. Configure and enable a source below to allow directory users.'}
        </p>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Source</Th>
                <Th>Status</Th>
                <Th>Server</Th>
                <Th>Role assignment</Th>
                <Th className="text-right">Accounts</Th>
                <Th className="w-[210px] text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {/* Local */}
              <Tr>
                <Td>
                  <SourceCell icon={HardDrive} title="Local accounts" subtitle="Built-in · passwords stored on this appliance" />
                </Td>
                <Td><StatusCell status="up" label="Always on" /></Td>
                <Td className="text-xs text-muted">This appliance</Td>
                <Td className="text-xs text-muted">Assigned per user</Td>
                <Td className="text-right text-sm tabular-nums">{counts.local}</Td>
                <Td />
              </Tr>

              {/* LDAP */}
              <Tr>
                <Td>
                  <SourceCell icon={Server} title="LDAP / Active Directory" subtitle="Service-account search, then bind as the user" />
                </Td>
                <Td>
                  {ldap.enabled
                    ? <StatusCell status="up" label="Enabled" />
                    : ldapConfigured(ldap)
                      ? <StatusCell status="idle" label="Disabled" />
                      : <StatusCell status="idle" label="Not configured" muted />}
                </Td>
                <Td>
                  {ldap.server ? (
                    <div className="text-sm">
                      <span className="font-mono text-xs">{ldap.server}:{ldap.port}</span>
                      <div className="text-[11px] text-muted">
                        {ldap.use_ssl ? 'LDAPS' : ldap.use_starttls ? 'StartTLS' : 'No encryption'}
                        {ldap.base_dn && <> · {ldap.base_dn}</>}
                      </div>
                    </div>
                  ) : <span className="text-xs text-muted">—</span>}
                </Td>
                <Td>
                  <MappingSummary
                    count={ldap.group_mappings.filter((m) => m.group && m.role).length}
                    unit="group"
                    defaultRole={ldap.default_role ? roleLabel[ldap.default_role] || ldap.default_role : null}
                    autoProvision={ldap.auto_provision}
                    configured={ldapConfigured(ldap)}
                  />
                </Td>
                <Td className="text-right text-sm tabular-nums">{counts.ldap}</Td>
                <Td>
                  <ProviderActions
                    enabled={ldap.enabled}
                    busy={toggle.isPending && toggle.variables?.kind === 'ldap'}
                    canTest={ldapConfigured(ldap)}
                    onTest={() => open('ldap', 'test')}
                    onConfigure={() => open('ldap')}
                    onToggle={(v) => onToggle('ldap', v)}
                  />
                </Td>
              </Tr>

              {/* RADIUS */}
              <Tr>
                <Td>
                  <SourceCell icon={Network} title="RADIUS" subtitle="PAP Access-Request · Class / Filter-Id reply attributes" />
                </Td>
                <Td>
                  {radius.enabled
                    ? <StatusCell status="up" label="Enabled" />
                    : radiusConfigured(radius)
                      ? <StatusCell status="idle" label="Disabled" />
                      : <StatusCell status="idle" label="Not configured" muted />}
                </Td>
                <Td>
                  {radius.server ? (
                    <div className="text-sm">
                      <span className="font-mono text-xs">{radius.server}:{radius.port}</span>
                      <div className="text-[11px] text-muted">
                        Timeout {radius.timeout}s · {radius.retries} {radius.retries === 1 ? 'retry' : 'retries'}
                        {!radius.has_secret && <span className="text-warning"> · no shared secret</span>}
                      </div>
                    </div>
                  ) : <span className="text-xs text-muted">—</span>}
                </Td>
                <Td>
                  <MappingSummary
                    count={radius.class_mappings.filter((m) => m.value && m.role).length}
                    unit="attribute"
                    defaultRole={radius.default_role ? roleLabel[radius.default_role] || radius.default_role : null}
                    autoProvision={radius.auto_provision}
                    configured={radiusConfigured(radius)}
                  />
                </Td>
                <Td className="text-right text-sm tabular-nums">{counts.radius}</Td>
                <Td>
                  <ProviderActions
                    enabled={radius.enabled}
                    busy={toggle.isPending && toggle.variables?.kind === 'radius'}
                    canTest={radiusConfigured(radius)}
                    onTest={() => open('radius', 'test')}
                    onConfigure={() => open('radius')}
                    onToggle={(v) => onToggle('radius', v)}
                  />
                </Td>
              </Tr>
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <p className="text-[11px] text-muted">
        Directory users are matched to a role on every sign-in through the source's mapping rules. Local accounts are never
        affected by external sources, and an account created by one source cannot sign in through another.
      </p>

      <LdapDialog
        open={editing === 'ldap'}
        onOpenChange={(o) => !o && setEditing(null)}
        initial={ldap}
        roles={roles || []}
        initialTab={editingTab}
      />
      <RadiusDialog
        open={editing === 'radius'}
        onOpenChange={(o) => !o && setEditing(null)}
        initial={radius}
        roles={roles || []}
        initialTab={editingTab}
      />
    </div>
  )
}

// ─── List cells ─────────────────────────────────────────────────────────────

function OrderStep({ label, active }: { label: string; active: boolean }) {
  return (
    <span className={cn(
      'inline-flex items-center gap-1.5 rounded border px-2 py-0.5',
      active ? 'border-primary/30 bg-primary/10 text-text' : 'border-border text-muted opacity-70',
    )}>
      <StatusDot status={active ? 'up' : 'idle'} />
      {label}
    </span>
  )
}

function SourceCell({ icon: Icon, title, subtitle }: { icon: typeof Server; title: string; subtitle: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface2/60">
        <Icon className="h-4 w-4 text-primary" />
      </div>
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        <div className="truncate text-[11px] text-muted">{subtitle}</div>
      </div>
    </div>
  )
}

function StatusCell({ status, label, muted }: { status: 'up' | 'idle' | 'warn'; label: string; muted?: boolean }) {
  return (
    <span className={cn('inline-flex items-center gap-2 text-sm', muted && 'text-muted')}>
      <StatusDot status={status} /> {label}
    </span>
  )
}

function MappingSummary({ count, unit, defaultRole, autoProvision, configured }: {
  count: number; unit: string; defaultRole: string | null; autoProvision: boolean; configured: boolean
}) {
  if (!configured) return <span className="text-xs text-muted">—</span>
  return (
    <div className="text-xs">
      <div>
        {count > 0
          ? <>{count} {unit} {count === 1 ? 'rule' : 'rules'}</>
          : <span className="text-muted">No {unit} rules</span>}
        <span className="text-muted"> · default </span>
        {defaultRole
          ? <span className="font-medium">{defaultRole}</span>
          : <span className="text-warning">deny</span>}
      </div>
      <div className="text-[11px] text-muted">{autoProvision ? 'Accounts created on first sign-in' : 'Existing accounts only'}</div>
    </div>
  )
}

function ProviderActions({ enabled, busy, canTest, onTest, onConfigure, onToggle }: {
  enabled: boolean; busy: boolean; canTest: boolean
  onTest: () => void; onConfigure: () => void; onToggle: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-end gap-1.5">
      <Button variant="ghost" size="sm" disabled={!canTest} onClick={onTest} title={canTest ? 'Test connection' : 'Configure the server first'}>
        <FlaskConical className="h-3.5 w-3.5" /> Test
      </Button>
      <Button variant="outline" size="sm" onClick={onConfigure}>
        <Settings2 className="h-3.5 w-3.5" /> Configure
      </Button>
      <div className="ml-1 flex items-center" title={enabled ? 'Disable' : 'Enable'}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin text-muted" /> : <Switch checked={enabled} onCheckedChange={(v) => onToggle(!!v)} />}
      </div>
    </div>
  )
}

// ─── Shared form pieces ─────────────────────────────────────────────────────

function RoleSelect({ value, onChange, roles, allowNone }: {
  value: string; onChange: (v: string) => void; roles: Role[]; allowNone?: boolean
}) {
  return (
    <Select value={value || (allowNone ? '__none__' : value)} onValueChange={(v) => onChange(v === '__none__' ? '' : v)}>
      <SelectTrigger><SelectValue /></SelectTrigger>
      <SelectContent>
        {allowNone && <SelectItem value="__none__">Deny sign-in</SelectItem>}
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
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-sm">
        <thead className="bg-surface2/50 text-[11px] uppercase tracking-wider text-muted">
          <tr>
            <th className="px-3 py-2 text-left font-medium">{keyLabel}</th>
            <th className="w-48 px-3 py-2 text-left font-medium">Role</th>
            <th className="w-10" />
          </tr>
        </thead>
        <tbody>
          {mappings.length === 0 && (
            <tr><td colSpan={3} className="px-3 py-4 text-center text-xs text-muted">No rules. Users fall through to the default role.</td></tr>
          )}
          {mappings.map((m, i) => (
            <tr key={i} className="border-t border-border">
              <td className="p-1.5">
                <Input
                  className="font-mono text-xs"
                  placeholder={keyPlaceholder}
                  value={(m as any)[keyField] || ''}
                  onChange={(e) => setMappings(mappings.map((x, j) => (j === i ? { ...x, [keyField]: e.target.value } : x)))}
                />
              </td>
              <td className="p-1.5">
                <RoleSelect
                  value={m.role}
                  roles={roles}
                  onChange={(v) => setMappings(mappings.map((x, j) => (j === i ? { ...x, role: v } : x)))}
                />
              </td>
              <td className="p-1.5 text-right">
                <Button type="button" variant="ghost" size="icon" className="h-8 w-8 text-muted hover:text-danger"
                  onClick={() => setMappings(mappings.filter((_, j) => j !== i))}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-border bg-surface2/30 px-2 py-1.5">
        <Button type="button" variant="ghost" size="sm"
          onClick={() => setMappings([...mappings, { [keyField]: '', role: roles[0]?.name || 'viewer' } as Mapping])}>
          <Plus className="h-3.5 w-3.5" /> Add rule
        </Button>
      </div>
    </div>
  )
}

function ToggleRow({ title, description, checked, onChange }: {
  title: string; description?: string; checked: boolean; onChange: (v: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 rounded-md border border-border px-3 py-2">
      <div>
        <div className="text-sm font-medium">{title}</div>
        {description && <div className="text-[11px] text-muted">{description}</div>}
      </div>
      <Switch checked={checked} onCheckedChange={(v) => onChange(!!v)} />
    </label>
  )
}

function Advanced({ open, onToggle, children }: { open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <div className="rounded-md border border-border">
      <button type="button" onClick={onToggle}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-muted hover:text-text">
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />} Advanced
      </button>
      {open && <div className="border-t border-border p-3">{children}</div>}
    </div>
  )
}

/** Number field that lets the admin clear it while typing; the caller
 * coerces blanks back to a default on save. */
function numValue(v: number | '' ) { return v === '' ? '' : String(v) }
function numParse(s: string): number | '' { return s === '' ? '' : Math.max(0, Math.floor(Number(s) || 0)) }

function TestPanel({ kind, payload, requireCreds, roleLabel }: {
  kind: ProviderKind
  payload: () => Record<string, unknown>
  requireCreds?: boolean
  roleLabel: Record<string, string>
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [result, setResult] = useState<TestResult | null>(null)

  const test = useMutation({
    mutationFn: async () => (await api.post(`/system/auth/${kind}/test`, { username, password, [kind]: payload() })).data as TestResult,
    onSuccess: (r) => setResult(r),
    onError: (e: any) => setResult({ success: false, message: apiErrorMessage(e) }),
  })

  const mapped = result?.mapped_role ? roleLabel[result.mapped_role] || result.mapped_role : null

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">
        {kind === 'ldap'
          ? 'Runs against the values currently in this dialog, saved or not. Leave the credentials blank to check reachability and the service-account bind only; enter a directory user to verify lookup, password, and the role that user would receive.'
          : 'Runs against the values currently in this dialog, saved or not. RADIUS needs a real user: an Access-Request is sent and the reply attributes are matched to a role.'}
      </p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
        <FormField label="Username">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder={requireCreds ? 'jsmith' : 'optional'} autoComplete="off" />
        </FormField>
        <FormField label="Password">
          <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </FormField>
        <Button type="button" variant="outline" disabled={test.isPending || (requireCreds && (!username || !password))} onClick={() => test.mutate()}>
          {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />} Run test
        </Button>
      </div>

      {result && (
        <div className={cn(
          'rounded-md border p-3 text-sm',
          result.success ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5',
        )}>
          <div className={cn('flex items-center gap-2 font-medium', result.success ? 'text-success' : 'text-danger')}>
            {result.success ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
            {result.message}
          </div>
          {result.success && (result.groups || result.reply_values || result.mapped_role !== undefined) && (
            <dl className="mt-2 grid grid-cols-[120px_1fr] gap-y-1 text-xs">
              {result.full_name && <><dt className="text-muted">Name</dt><dd>{result.full_name}</dd></>}
              {result.email && <><dt className="text-muted">Email</dt><dd>{result.email}</dd></>}
              <dt className="text-muted">Would sign in</dt>
              <dd>{result.would_sign_in ? <span className="text-success">Yes</span> : <span className="text-danger">No — no role matched</span>}</dd>
              <dt className="text-muted">Role</dt>
              <dd className="font-medium">{mapped || <span className="text-muted">none</span>}</dd>
              {result.groups && (
                <>
                  <dt className="text-muted">Groups</dt>
                  <dd className="flex flex-wrap gap-1">
                    {result.groups.length === 0 && <span className="text-muted">(none)</span>}
                    {result.groups.map((g) => <span key={g} className="rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-[10px]">{g}</span>)}
                  </dd>
                </>
              )}
              {result.reply_values && (
                <>
                  <dt className="text-muted">Reply attributes</dt>
                  <dd className="flex flex-wrap gap-1">
                    {result.reply_values.length === 0 && <span className="text-muted">(none)</span>}
                    {result.reply_values.map((v) => <span key={v} className="rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-[10px]">{v}</span>)}
                  </dd>
                </>
              )}
            </dl>
          )}
        </div>
      )}
    </div>
  )
}

function DialogTabs({ showTest }: { showTest: boolean }) {
  return (
    <TabsList className="h-9 bg-surface2/50 p-1">
      <TabsTrigger value="connection" className="text-xs">Connection</TabsTrigger>
      <TabsTrigger value="mapping" className="text-xs">Role mapping</TabsTrigger>
      <TabsTrigger value="test" className="text-xs" disabled={!showTest}>Test</TabsTrigger>
    </TabsList>
  )
}

// ─── LDAP dialog ────────────────────────────────────────────────────────────

type LdapForm = Omit<LdapConfig, 'port'> & { port: number | '' }

function LdapDialog({ open, onOpenChange, initial, roles, initialTab }: {
  open: boolean; onOpenChange: (o: boolean) => void; initial: LdapConfig; roles: Role[]
  initialTab: 'connection' | 'mapping' | 'test'
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState<LdapForm>(initial)
  const [tab, setTab] = useState(initialTab)
  const [advanced, setAdvanced] = useState(false)

  // Initialise only when the dialog opens — a background refetch must not
  // wipe half-typed values.
  useEffect(() => {
    if (!open) return
    setForm({ ...initial, bind_password: '' })
    setTab(initialTab)
    setAdvanced(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const f = (patch: Partial<LdapForm>) => setForm((prev) => ({ ...prev, ...patch }))
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])

  const encryption = form.use_ssl ? 'ldaps' : form.use_starttls ? 'starttls' : 'none'
  function setEncryption(v: string) {
    const patch: Partial<LdapForm> = { use_ssl: v === 'ldaps', use_starttls: v === 'starttls' }
    // Only swap the port when it is still a well-known default; a custom port stays.
    if (form.port === '' || LDAP_DEFAULT_PORTS.has(form.port)) patch.port = v === 'ldaps' ? 636 : 389
    f(patch)
  }

  const payload = () => ({
    ...form,
    port: form.port === '' ? (form.use_ssl ? 636 : 389) : form.port,
    group_mappings: form.group_mappings.filter((m) => (m.group || '').trim()),
  })
  const canTest = !!form.server && !!form.base_dn
  const missing = form.enabled && (!form.server || !form.base_dn)

  const save = useMutation({
    mutationFn: async () => (await api.put('/system/auth/ldap', payload())).data,
    onSuccess: () => {
      toast.success('LDAP / Active Directory saved')
      qc.invalidateQueries({ queryKey: ['auth', 'providers'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Server className="h-4 w-4 text-primary" /> LDAP / Active Directory</DialogTitle>
          <DialogDescription>Directory sign-in for users without a local account. Nothing is applied until you save.</DialogDescription>
        </DialogHeader>

        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">
          <Tabs value={tab} onValueChange={(v) => setTab(v as any)}>
            <DialogTabs showTest={canTest} />

            <TabsContent value="connection" className="mt-3 space-y-3">
              <ToggleRow
                title="Enable LDAP / Active Directory sign-in"
                description="Unknown usernames are looked up in the directory when they sign in."
                checked={form.enabled}
                onChange={(v) => f({ enabled: v })}
              />
              {missing && <p className="text-[11px] text-warning">Server and Base DN are required before this source can be enabled.</p>}

              <div className="grid grid-cols-1 gap-3 md:grid-cols-6">
                <FormField label="Server" required className="md:col-span-3">
                  <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="dc01.corp.local" />
                </FormField>
                <FormField label="Port">
                  <Input type="number" min={1} max={65535} value={numValue(form.port)} onChange={(e) => f({ port: numParse(e.target.value) })} />
                </FormField>
                <FormField label="Encryption" className="md:col-span-2">
                  <Select value={encryption} onValueChange={setEncryption}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="ldaps">LDAPS (TLS, port 636)</SelectItem>
                      <SelectItem value="starttls">StartTLS (port 389)</SelectItem>
                      <SelectItem value="none">None (plaintext)</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <FormField label="Bind DN" hint="Service account used to search the directory">
                  <Input value={form.bind_dn} onChange={(e) => f({ bind_dn: e.target.value })} placeholder="CN=svc-zenplus,OU=Service,DC=corp,DC=local" autoComplete="off" className="font-mono text-xs" />
                </FormField>
                <FormField label="Bind password" hint={form.has_bind_password ? 'Saved. Leave blank to keep the stored password.' : undefined}>
                  <PasswordInput value={form.bind_password} onChange={(e) => f({ bind_password: e.target.value })} hasStored={form.has_bind_password} />
                </FormField>
                <FormField label="Base DN" required hint="Search root for user lookups" className="md:col-span-2">
                  <Input value={form.base_dn} onChange={(e) => f({ base_dn: e.target.value })} placeholder="DC=corp,DC=local" className="font-mono text-xs" />
                </FormField>
              </div>

              <Advanced open={advanced} onToggle={() => setAdvanced((v) => !v)}>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="User filter" hint="{username} is replaced with the login name">
                    <Input value={form.user_filter} onChange={(e) => f({ user_filter: e.target.value })} placeholder="(sAMAccountName={username})" className="font-mono text-xs" />
                  </FormField>
                  <div className="grid grid-cols-3 gap-2">
                    <FormField label="Email attribute"><Input value={form.email_attr} onChange={(e) => f({ email_attr: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Name attribute"><Input value={form.name_attr} onChange={(e) => f({ name_attr: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Group attribute"><Input value={form.group_attr} onChange={(e) => f({ group_attr: e.target.value })} className="font-mono text-xs" /></FormField>
                  </div>
                </div>
                <p className="mt-2 text-[11px] text-muted">
                  Defaults suit Active Directory. For OpenLDAP use <code className="font-mono">(uid={'{username}'})</code> and the <code className="font-mono">cn</code> name attribute.
                </p>
              </Advanced>
            </TabsContent>

            <TabsContent value="mapping" className="mt-3 space-y-3">
              <p className="text-xs text-muted">
                Rules are evaluated top to bottom against the user's group memberships; the first match assigns the role.
                A group can be given as a full DN or just its CN.
              </p>
              <MappingRows
                mappings={form.group_mappings}
                setMappings={(m) => f({ group_mappings: m })}
                keyField="group" keyLabel="Directory group"
                keyPlaceholder="CN=NetAdmins,OU=Groups,DC=corp,DC=local"
                roles={roles}
              />
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <FormField label="When no rule matches" hint="Deny keeps unmapped directory users out entirely.">
                  <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone />
                </FormField>
                <div className="flex flex-col justify-end">
                  <ToggleRow
                    title="Create accounts on first sign-in"
                    description="Off: only users who already have an LDAP account here can sign in."
                    checked={form.auto_provision}
                    onChange={(v) => f({ auto_provision: v })}
                  />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="test" className="mt-3">
              <TestPanel kind="ldap" payload={payload} roleLabel={roleLabel} />
            </TabsContent>
          </Tabs>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || missing}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ─── RADIUS dialog ──────────────────────────────────────────────────────────

type RadiusForm = Omit<RadiusConfig, 'port' | 'timeout' | 'retries'> & { port: number | ''; timeout: number | ''; retries: number | '' }

function RadiusDialog({ open, onOpenChange, initial, roles, initialTab }: {
  open: boolean; onOpenChange: (o: boolean) => void; initial: RadiusConfig; roles: Role[]
  initialTab: 'connection' | 'mapping' | 'test'
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState<RadiusForm>(initial)
  const [tab, setTab] = useState(initialTab)
  const [advanced, setAdvanced] = useState(false)

  useEffect(() => {
    if (!open) return
    setForm({ ...initial, secret: '' })
    setTab(initialTab)
    setAdvanced(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const f = (patch: Partial<RadiusForm>) => setForm((prev) => ({ ...prev, ...patch }))
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])

  const payload = () => ({
    ...form,
    port: form.port === '' ? 1812 : form.port,
    timeout: form.timeout === '' ? 5 : Math.min(60, Math.max(1, form.timeout)),
    retries: form.retries === '' ? 3 : Math.min(10, Math.max(1, form.retries)),
    class_mappings: form.class_mappings.filter((m) => (m.value || '').trim()),
  })
  const hasSecret = !!form.secret || !!form.has_secret
  const canTest = !!form.server && hasSecret
  const missing = form.enabled && (!form.server || !hasSecret)

  const save = useMutation({
    mutationFn: async () => (await api.put('/system/auth/radius', payload())).data,
    onSuccess: () => {
      toast.success('RADIUS saved')
      qc.invalidateQueries({ queryKey: ['auth', 'providers'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Network className="h-4 w-4 text-primary" /> RADIUS</DialogTitle>
          <DialogDescription>PAP authentication against a RADIUS server. Nothing is applied until you save.</DialogDescription>
        </DialogHeader>

        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">
          <Tabs value={tab} onValueChange={(v) => setTab(v as any)}>
            <DialogTabs showTest={canTest} />

            <TabsContent value="connection" className="mt-3 space-y-3">
              <ToggleRow
                title="Enable RADIUS sign-in"
                description="Tried after LDAP / Active Directory for unknown usernames."
                checked={form.enabled}
                onChange={(v) => f({ enabled: v })}
              />
              {missing && <p className="text-[11px] text-warning">Server and shared secret are required before this source can be enabled.</p>}

              <div className="grid grid-cols-1 gap-3 md:grid-cols-6">
                <FormField label="Server" required className="md:col-span-3">
                  <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="radius.corp.local" />
                </FormField>
                <FormField label="Auth port">
                  <Input type="number" min={1} max={65535} value={numValue(form.port)} onChange={(e) => f({ port: numParse(e.target.value) })} />
                </FormField>
                <FormField label="Shared secret" required className="md:col-span-2" hint={form.has_secret ? 'Saved. Leave blank to keep the stored secret.' : undefined}>
                  <PasswordInput value={form.secret} onChange={(e) => f({ secret: e.target.value })} hasStored={form.has_secret} />
                </FormField>
              </div>

              <Advanced open={advanced} onToggle={() => setAdvanced((v) => !v)}>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                  <FormField label="Timeout (seconds)" hint="1–60">
                    <Input type="number" min={1} max={60} value={numValue(form.timeout)} onChange={(e) => f({ timeout: numParse(e.target.value) })} />
                  </FormField>
                  <FormField label="Retries" hint="1–10">
                    <Input type="number" min={1} max={10} value={numValue(form.retries)} onChange={(e) => f({ retries: numParse(e.target.value) })} />
                  </FormField>
                  <FormField label="NAS-Identifier" hint="Sent in every Access-Request">
                    <Input value={form.nas_identifier} onChange={(e) => f({ nas_identifier: e.target.value })} placeholder="zenplus" className="font-mono text-xs" />
                  </FormField>
                </div>
              </Advanced>
            </TabsContent>

            <TabsContent value="mapping" className="mt-3 space-y-3">
              <p className="text-xs text-muted">
                Rules are matched against the Class, Filter-Id, and Reply-Message attributes returned on Access-Accept;
                the first match assigns the role.
              </p>
              <MappingRows
                mappings={form.class_mappings}
                setMappings={(m) => f({ class_mappings: m })}
                keyField="value" keyLabel="Reply attribute value"
                keyPlaceholder="netadmins"
                roles={roles}
              />
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <FormField label="When no rule matches" hint="Deny keeps unmapped RADIUS users out entirely.">
                  <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone />
                </FormField>
                <div className="flex flex-col justify-end">
                  <ToggleRow
                    title="Create accounts on first sign-in"
                    description="Off: only users who already have a RADIUS account here can sign in."
                    checked={form.auto_provision}
                    onChange={(v) => f({ auto_provision: v })}
                  />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="test" className="mt-3">
              <TestPanel kind="radius" payload={payload} requireCreds roleLabel={roleLabel} />
            </TabsContent>
          </Tabs>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || missing}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
