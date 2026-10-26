import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, ChevronDown, ChevronRight, FlaskConical, Loader2, Network,
  Plus, RefreshCw, Server, ShieldAlert, Trash2, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { describeFailure } from '@/lib/access'
import type { Role } from '@/types'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'
import { copyText } from '@/lib/utils'
import { KeyRound, Copy } from 'lucide-react'

// Backend: server/app/api/v1/auth_settings.py (/api/v1/system/auth/*)

export type Mapping = { group?: string; value?: string; attribute?: string; role: string }

type Meta = { revision?: number; updated_at?: string; updated_by?: string }

export type LdapConfig = Meta & {
  enabled: boolean; server: string; port: number; use_ssl: boolean; use_starttls: boolean
  ca_certificate_pem?: string; bind_dn: string; bind_password: string; has_bind_password?: boolean
  base_dn: string; user_filter: string; email_attr: string; name_attr: string; group_attr: string
  group_mappings: Mapping[]; default_role: string; auto_provision: boolean
}

export type RadiusConfig = Meta & {
  enabled: boolean; server: string; port: number; secret: string; has_secret?: boolean
  timeout: number; retries: number; nas_identifier: string
  class_mappings: Mapping[]; default_role: string; auto_provision: boolean
}

export type OidcConfig = Meta & {
  enabled: boolean; display_name: string; issuer: string; client_id: string; client_secret: string; has_client_secret?: boolean
  scopes: string; username_claim: string; email_claim: string; name_claim: string; groups_claim: string
  group_mappings: Mapping[]; default_role: string; auto_provision: boolean; ca_certificate_pem?: string; redirect_base_url: string
}

export type ProviderKind = 'ldap' | 'radius' | 'oidc'
export type DialogTab = 'connection' | 'mapping' | 'test'

type TestResult = {
  success: boolean; message: string
  dn?: string; email?: string | null; full_name?: string | null
  groups?: string[]; mapped_role?: string | null
  matched_rule?: { kind: 'mapping' | 'default'; index?: number; match?: string; attribute?: string | null } | null
  would_sign_in?: boolean; admission_reasons?: { code: string; message: string }[]
  mapped_role_privileged?: boolean
  reply_values?: string[]; reply_attributes?: { attribute: string; value: string }[]
  steps?: { name: string; ok: boolean; message?: string }[]
}

const LDAP_DEFAULT_PORTS = new Set([389, 636])
const RADIUS_ATTRIBUTES = ['Class', 'Filter-Id', 'Reply-Message']
const STEP_LABEL: Record<string, string> = { connection: 'Connection', credentials: 'Credentials', mapping: 'Role mapping', admission: 'ZenPlus admission' }

export const ldapConfigured = (c: LdapConfig) => !!c.server && !!c.base_dn
export const radiusConfigured = (c: RadiusConfig) => !!c.server && !!(c.secret || c.has_secret)
export const oidcConfigured = (c: OidcConfig) => !!c.issuer && !!c.client_id && !!(c.client_secret || c.has_client_secret)

// ─── Shared form pieces ─────────────────────────────────────────────────────

/** Role picker for mapping rules and defaults. No implicit choice: a new rule
 *  starts at "Choose role", and roles that carry access-control powers are
 *  flagged so a privileged grant is always a deliberate pick. */
function RoleSelect({ value, onChange, roles, allowNone, label, noneLabel = 'Deny sign-in', placeholder = 'Choose role…' }: {
  value: string; onChange: (v: string) => void; roles: Role[]; allowNone?: boolean; label: string; noneLabel?: string; placeholder?: string
}) {
  return (
    <Select value={value || (allowNone ? '__none__' : '')} onValueChange={(v) => onChange(v === '__none__' ? '' : v)}>
      <SelectTrigger aria-label={label}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent>
        {allowNone && <SelectItem value="__none__">{noneLabel}</SelectItem>}
        {roles.map((r) => (
          <SelectItem key={r.name} value={r.name}>
            {r.display_name}{r.permissions.includes('system.admin') ? ' — full administration' : r.privileged ? ' — can manage access' : ''}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function isPrivilegedRole(roles: Role[], name: string) {
  const r = roles.find((x) => x.name === name)
  return !!r && (r.permissions.includes('system.admin') || !!r.privileged)
}

type RowProblem = { row: number; message: string }

/** First-match-wins rule list with explicit ordering, labelled controls and
 *  inline validation; incomplete rows are reported instead of silently dropped. */
function MappingRows({ mappings, setMappings, keyField, keyLabel, keyPlaceholder, roles, withAttribute, problems }: {
  mappings: Mapping[]
  setMappings: (m: Mapping[]) => void
  keyField: 'group' | 'value'
  keyLabel: string
  keyPlaceholder: string
  roles: Role[]
  withAttribute?: boolean
  problems: RowProblem[]
}) {
  const move = (i: number, d: -1 | 1) => {
    const j = i + d
    if (j < 0 || j >= mappings.length) return
    const next = [...mappings]; [next[i], next[j]] = [next[j], next[i]]
    setMappings(next)
  }
  const patch = (i: number, p: Partial<Mapping>) => setMappings(mappings.map((x, j) => (j === i ? { ...x, ...p } : x)))
  const rowProblem = (i: number) => problems.find((p) => p.row === i + 1)?.message

  return (
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-sm">
        <caption className="sr-only">Role mapping rules, evaluated top to bottom; the first match wins</caption>
        <thead className="bg-surface2/50 text-xs uppercase tracking-wider text-muted">
          <tr>
            <th scope="col" className="w-10 px-2 py-2 text-center font-medium">#</th>
            <th scope="col" className="px-3 py-2 text-left font-medium">{keyLabel}</th>
            {withAttribute && <th scope="col" className="w-36 px-3 py-2 text-left font-medium">Attribute</th>}
            <th scope="col" className="w-52 px-3 py-2 text-left font-medium">Role</th>
            <th scope="col" className="w-28 px-2 py-2 text-right font-medium"><span className="sr-only">Row actions</span></th>
          </tr>
        </thead>
        <tbody>
          {mappings.length === 0 && (
            <tr><td colSpan={withAttribute ? 5 : 4} className="px-3 py-4 text-center text-xs text-muted">No rules. Users fall through to “When no rule matches” below.</td></tr>
          )}
          {mappings.map((m, i) => {
            const problem = rowProblem(i)
            const priv = m.role && isPrivilegedRole(roles, m.role)
            return (
              <tr key={i} className={cn('border-t border-border align-top', problem && 'bg-danger/[0.04]')}>
                <td className="px-2 py-2.5 text-center text-xs text-muted">{i + 1}</td>
                <td className="p-1.5">
                  <Input
                    className="font-mono text-xs"
                    aria-label={`Rule ${i + 1} ${keyLabel.toLowerCase()}`}
                    aria-invalid={!!problem}
                    placeholder={keyPlaceholder}
                    value={(m as any)[keyField] || ''}
                    onChange={(e) => patch(i, { [keyField]: e.target.value } as Partial<Mapping>)}
                  />
                  {problem && <p role="alert" className="mt-1 text-xs text-danger">{problem}</p>}
                </td>
                {withAttribute && (
                  <td className="p-1.5">
                    <Select value={m.attribute || '__any__'} onValueChange={(v) => patch(i, { attribute: v === '__any__' ? '' : v })}>
                      <SelectTrigger aria-label={`Rule ${i + 1} attribute`}><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__any__">Any attribute</SelectItem>
                        {RADIUS_ATTRIBUTES.map((a) => <SelectItem key={a} value={a}>{a}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </td>
                )}
                <td className="p-1.5">
                  <RoleSelect value={m.role} roles={roles} label={`Rule ${i + 1} role`} onChange={(v) => patch(i, { role: v })} />
                  {priv && <p className="mt-1 flex items-center gap-1 text-2xs text-warning"><ShieldAlert className="h-3 w-3" aria-hidden /> Privileged role</p>}
                </td>
                <td className="p-1.5 text-right">
                  <div className="flex justify-end">
                    <Button type="button" variant="ghost" size="icon" className="h-8 w-8" aria-label={`Move rule ${i + 1} up`} disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp className="h-3.5 w-3.5" /></Button>
                    <Button type="button" variant="ghost" size="icon" className="h-8 w-8" aria-label={`Move rule ${i + 1} down`} disabled={i === mappings.length - 1} onClick={() => move(i, 1)}><ArrowDown className="h-3.5 w-3.5" /></Button>
                    <Button type="button" variant="ghost" size="icon" className="h-8 w-8 text-muted hover:text-danger" aria-label={`Delete rule ${i + 1}`} onClick={() => setMappings(mappings.filter((_, j) => j !== i))}><Trash2 className="h-3.5 w-3.5" /></Button>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="border-t border-border bg-surface2/30 px-2 py-1.5">
        {/* A new rule has no role: granting access is always a deliberate choice. */}
        <Button type="button" variant="ghost" size="sm" onClick={() => setMappings([...mappings, { [keyField]: '', role: '' } as Mapping])}>
          <Plus className="h-3.5 w-3.5" /> Add rule
        </Button>
      </div>
    </div>
  )
}

function validateMappings(mappings: Mapping[], keyField: 'group' | 'value', noun: string, withAttribute: boolean): RowProblem[] {
  const out: RowProblem[] = []
  const seen = new Map<string, number>()
  mappings.forEach((m, i) => {
    const key = (m[keyField] || '').trim()
    if (!key && !m.role) return
    if (!key) { out.push({ row: i + 1, message: `Enter the ${noun} to match` }); return }
    if (!m.role) { out.push({ row: i + 1, message: 'Choose the role to grant' }); return }
    const id = `${key.toLowerCase()}|${withAttribute ? (m.attribute || '').toLowerCase() : ''}`
    if (seen.has(id)) out.push({ row: i + 1, message: `Duplicates rule ${seen.get(id)}; this rule can never match` })
    else seen.set(id, i + 1)
  })
  return out
}

function ToggleRow({ title, description, checked, onChange }: {
  title: string; description?: string; checked: boolean; onChange: (v: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 rounded-md border border-border px-3 py-2">
      <div>
        <div className="text-sm font-medium">{title}</div>
        {description && <div className="text-xs text-muted">{description}</div>}
      </div>
      <Switch checked={checked} onCheckedChange={(v) => onChange(!!v)} aria-label={title} />
    </label>
  )
}

function Advanced({ open, onToggle, children }: { open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <div className="rounded-md border border-border">
      <button type="button" onClick={onToggle} aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-muted hover:text-text">
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />} Advanced
      </button>
      {open && <div className="border-t border-border p-3">{children}</div>}
    </div>
  )
}

const numValue = (v: number | '') => (v === '' ? '' : String(v))
const numParse = (s: string): number | '' => (s === '' ? '' : Math.max(0, Math.floor(Number(s) || 0)))

/** Runs a test with the dialog's current (possibly unsaved) values. It has its
 *  own inputs inside the configuration form, so Enter here runs the TEST and
 *  never submits (saves) the surrounding form. */
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
    mutationFn: async () => (await api.post(`/system/auth/${kind}/test`, { username, password, [kind]: payload() }, { timeout: 70_000 })).data as TestResult,
    onSuccess: (r) => setResult(r),
    onError: (e: any) => setResult({ success: false, message: describeFailure(e).message }),
  })
  const canRun = !test.isPending && !(requireCreds && (!username || !password))
  const run = () => { if (canRun) test.mutate() }
  const onEnter = (e: React.KeyboardEvent) => { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); run() } }

  const mapped = result?.mapped_role ? roleLabel[result.mapped_role] || result.mapped_role : null
  const rule = result?.matched_rule
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">
        {kind === 'ldap'
          ? 'Runs against the values currently in this dialog, saved or not. Leave the credentials blank to check reachability and the service-account bind only; enter a directory user to verify lookup, password, the matching rule and whether ZenPlus would admit that user. Nothing is created or changed.'
          : 'Runs against the values currently in this dialog, saved or not. RADIUS needs a real user: an Access-Request is sent and the reply attributes are matched to a role. Nothing is created or changed.'}
      </p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
        <FormField label="Username">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} onKeyDown={onEnter} placeholder={requireCreds ? 'jsmith' : 'optional'} autoComplete="off" />
        </FormField>
        <FormField label="Password">
          <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={onEnter} autoComplete="new-password" />
        </FormField>
        <Button type="button" variant="outline" disabled={!canRun} onClick={run}>
          {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />} {test.isPending ? 'Testing…' : 'Run test'}
        </Button>
      </div>

      {result && (
        <div role="status" className={cn('rounded-md border p-3 text-sm', result.success ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5')}>
          <div className={cn('flex items-center gap-2 font-medium', result.success ? 'text-success' : 'text-danger')}>
            {result.success ? <CheckCircle2 className="h-4 w-4" aria-hidden /> : <XCircle className="h-4 w-4" aria-hidden />}
            {result.message}
          </div>
          {result.steps && result.steps.length > 0 && (
            <ol className="mt-2 flex flex-wrap gap-1.5 text-xs">
              {result.steps.map((s) => (
                <li key={s.name} className={cn('inline-flex items-center gap-1 rounded border px-1.5 py-0.5', s.ok ? 'border-success/30 text-success' : 'border-danger/30 text-danger')}>
                  {s.ok ? <CheckCircle2 className="h-3 w-3" aria-hidden /> : <XCircle className="h-3 w-3" aria-hidden />}{STEP_LABEL[s.name] || s.name}
                </li>
              ))}
            </ol>
          )}
          {result.success && (result.groups || result.reply_values || result.mapped_role !== undefined) && (
            <dl className="mt-2 grid grid-cols-[120px_1fr] gap-y-1 text-xs">
              {result.full_name && <><dt className="text-muted">Name</dt><dd>{result.full_name}</dd></>}
              {result.email && <><dt className="text-muted">Email</dt><dd>{result.email}</dd></>}
              <dt className="text-muted">Directory result</dt><dd className="text-success">Authenticated</dd>
              <dt className="text-muted">Role</dt>
              <dd className="font-medium">
                {mapped || <span className="text-muted">none</span>}
                {result.mapped_role_privileged && <span className="ml-2 inline-flex items-center gap-1 text-warning"><ShieldAlert className="h-3 w-3" aria-hidden /> privileged</span>}
              </dd>
              <dt className="text-muted">Matched rule</dt>
              <dd>{rule ? (rule.kind === 'default' ? 'No rule matched — the default role applies' : `Rule ${(rule.index ?? 0) + 1}: ${rule.match}${rule.attribute ? ` (${rule.attribute})` : ''}`) : <span className="text-muted">none</span>}</dd>
              <dt className="text-muted">ZenPlus would admit</dt>
              <dd>{result.would_sign_in ? <span className="text-success">Yes</span> : <span className="text-danger">No</span>}</dd>
              {result.admission_reasons && result.admission_reasons.length > 0 && (
                <>
                  <dt className="text-muted">Why not</dt>
                  <dd><ul className="list-disc pl-4 text-danger">{result.admission_reasons.map((r) => <li key={r.code}>{r.message}</li>)}</ul></dd>
                </>
              )}
              {result.groups && (
                <>
                  <dt className="text-muted">Groups</dt>
                  <dd className="flex flex-wrap gap-1">
                    {result.groups.length === 0 && <span className="text-muted">(none)</span>}
                    {result.groups.map((g) => <span key={g} className="rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-2xs">{g}</span>)}
                  </dd>
                </>
              )}
              {(result.reply_attributes || result.reply_values) && (
                <>
                  <dt className="text-muted">Reply attributes</dt>
                  <dd className="flex flex-wrap gap-1">
                    {(result.reply_attributes?.length ? result.reply_attributes : (result.reply_values || []).map((v) => ({ attribute: '', value: v }))).length === 0 && <span className="text-muted">(none)</span>}
                    {(result.reply_attributes?.length ? result.reply_attributes : (result.reply_values || []).map((v) => ({ attribute: '', value: v }))).map((a, i) => (
                      <span key={i} className="rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-2xs">{a.attribute ? `${a.attribute}=` : ''}{a.value}</span>
                    ))}
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

function DialogTabs({ showTest, mappingIssues }: { showTest: boolean; mappingIssues: number }) {
  return (
    <TabsList className="h-9 bg-surface2/50 p-1">
      <TabsTrigger value="connection" className="text-xs">Connection</TabsTrigger>
      <TabsTrigger value="mapping" className="text-xs">Role mapping{mappingIssues > 0 && <span className="ml-1 rounded-full bg-danger px-1.5 text-2xs text-white" aria-label={`${mappingIssues} problems`}>{mappingIssues}</span>}</TabsTrigger>
      <TabsTrigger value="test" className="text-xs" disabled={!showTest}>Test</TabsTrigger>
    </TabsList>
  )
}

// ─── Save handling shared by both dialogs ───────────────────────────────────

type SaveState = {
  conflict: any | null
  privileged: string[] | null
  error: string | null
}

function useProviderSave(kind: ProviderKind, build: (confirmPrivileged: boolean) => Record<string, unknown>, onDone: () => void) {
  const qc = useQueryClient()
  const [state, setState] = useState<SaveState>({ conflict: null, privileged: null, error: null })
  const mutation = useMutation({
    mutationFn: async (confirmPrivileged: boolean) => (await api.put(`/system/auth/${kind}`, build(confirmPrivileged))).data,
    onSuccess: () => {
      toast.success(kind === 'ldap' ? 'LDAP / Active Directory saved' : kind === 'radius' ? 'RADIUS saved' : 'Single sign-on saved')
      qc.invalidateQueries({ queryKey: ['auth'] })
      qc.invalidateQueries({ queryKey: ['roles'] })
      onDone()
    },
    onError: (e: any) => {
      const f = describeFailure(e)
      if (f.status === 409 && f.code === 'privileged_mapping_confirmation_required') setState({ conflict: null, privileged: f.detail?.roles || [], error: null })
      else if (f.status === 409 && f.code === 'provider_revision_conflict') setState({ conflict: f.detail?.current || {}, privileged: null, error: null })
      else if (f.status === 422 && f.detail?.problems) setState({ conflict: null, privileged: null, error: (f.detail.problems as string[]).join(' · ') })
      else setState({ conflict: null, privileged: null, error: f.message })
    },
  })
  return { ...state, save: (c = false) => { setState({ conflict: null, privileged: null, error: null }); mutation.mutate(c) }, isPending: mutation.isPending, reset: () => setState({ conflict: null, privileged: null, error: null }) }
}

function SaveBanners({ s, roleLabel, onConfirm, onReload }: {
  s: ReturnType<typeof useProviderSave>; roleLabel: Record<string, string>; onConfirm: () => void; onReload: () => void
}) {
  return (
    <>
      {s.error && <div role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">{s.error}</div>}
      {s.conflict && (
        <div role="alert" className="space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          <div className="flex items-center gap-2 font-medium"><AlertTriangle className="h-4 w-4 text-warning" /> Another administrator changed this source</div>
          <p className="text-xs text-muted">Saved by {s.conflict.updated_by || 'someone'}{s.conflict.revision ? ` (revision ${s.conflict.revision})` : ''} since you opened it. Reload to see their settings; your edits in this dialog will be discarded.</p>
          <Button type="button" size="sm" variant="outline" onClick={onReload}><RefreshCw className="h-3.5 w-3.5" /> Reload latest</Button>
        </div>
      )}
      {s.privileged && (
        <div role="alertdialog" aria-label="Confirm privileged grant" className="space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          <div className="flex items-center gap-2 font-medium"><ShieldAlert className="h-4 w-4 text-warning" /> This grants privileged access to directory users</div>
          <p className="text-xs">Everyone matched by these rules will receive: <strong>{s.privileged.map((r) => roleLabel[r] || r).join(', ')}</strong>. Directory group membership then decides who can administer ZenPlus.</p>
          <div className="flex gap-2">
            <Button type="button" size="sm" variant="destructive" onClick={onConfirm}>I understand — save</Button>
            <Button type="button" size="sm" variant="outline" onClick={s.reset}>Review again</Button>
          </div>
        </div>
      )}
    </>
  )
}

// ─── LDAP dialog ────────────────────────────────────────────────────────────

type LdapForm = Omit<LdapConfig, 'port'> & { port: number | '' }

export function LdapDialog({ open, onOpenChange, initial, roles, rolesFailed, initialTab, onReload }: {
  open: boolean; onOpenChange: (o: boolean) => void; initial: LdapConfig; roles: Role[]; rolesFailed?: boolean
  initialTab: DialogTab; onReload: () => void
}) {
  const [form, setForm] = useState<LdapForm>(initial)
  const [baseline, setBaseline] = useState<string>('')
  const [tab, setTab] = useState<DialogTab>(initialTab)
  const [advanced, setAdvanced] = useState(false)
  const [clearSecret, setClearSecret] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  // Initialise only when the dialog opens — a background refetch must not
  // wipe half-typed values.
  useEffect(() => {
    if (!open) return
    const f = { ...initial, bind_password: '' } as LdapForm
    setForm(f)
    setBaseline(JSON.stringify({ ...f }))
    setTab(initialTab); setAdvanced(false); setClearSecret(false); setSubmitted(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const f = (patch: Partial<LdapForm>) => setForm((prev) => ({ ...prev, ...patch }))
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])
  const encryption = form.use_ssl ? 'ldaps' : form.use_starttls ? 'starttls' : 'none'
  function setEncryption(v: string) {
    const patch: Partial<LdapForm> = { use_ssl: v === 'ldaps', use_starttls: v === 'starttls' }
    if (form.port === '' || LDAP_DEFAULT_PORTS.has(form.port)) patch.port = v === 'ldaps' ? 636 : 389
    f(patch)
  }

  const problems = validateMappings(form.group_mappings, 'group', 'group', false)
  const payload = (confirmPrivileged = false) => ({
    ...form,
    port: form.port === '' ? (form.use_ssl ? 636 : 389) : form.port,
    // Incomplete rows are reported (below), never silently dropped; blank rows are just placeholders.
    group_mappings: form.group_mappings.filter((m) => (m.group || '').trim() || m.role),
    expected_revision: initial.revision,
    clear_secret: clearSecret,
    confirm_privileged: confirmPrivileged,
  })
  const canTest = !!form.server && !!form.base_dn
  const missing = form.enabled && (!form.server || !form.base_dn)
  const dirty = open && JSON.stringify({ ...form }) !== baseline || clearSecret
  const saver = useProviderSave('ldap', (c) => payload(c) as Record<string, unknown>, () => onOpenChange(false))
  const guard = useDirtyGuard(dirty, saver.isPending, onOpenChange)
  const blocked = problems.length > 0 || missing || (rolesFailed && form.group_mappings.length > 0)

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><Server className="h-4 w-4 text-primary" /> LDAP / Active Directory</DialogTitle>
            <DialogDescription>Directory sign-in for users without a local account. Nothing is applied until you save.</DialogDescription>
          </DialogHeader>

          <form noValidate onSubmit={(e) => { e.preventDefault(); setSubmitted(true); if (!blocked) saver.save() }} className="space-y-4">
            <SaveBanners s={saver} roleLabel={roleLabel} onConfirm={() => saver.save(true)} onReload={() => { saver.reset(); onOpenChange(false); onReload() }} />
            <Tabs value={tab} onValueChange={(v) => setTab(v as DialogTab)}>
              <DialogTabs showTest={canTest} mappingIssues={problems.length} />

              <TabsContent value="connection" className="mt-3 space-y-3">
                <ToggleRow title="Enable LDAP / Active Directory sign-in" description="Unknown usernames are looked up in the directory when they sign in." checked={form.enabled} onChange={(v) => f({ enabled: v })} />
                {missing && <p role="alert" className="text-xs text-warning">Server and Base DN are required before this source can be enabled.</p>}

                <div className="grid grid-cols-1 gap-3 md:grid-cols-6">
                  <FormField label="Server" required className="md:col-span-3">
                    <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="dc01.corp.local" />
                  </FormField>
                  <FormField label="Port">
                    <Input type="number" min={1} max={65535} value={numValue(form.port)} onChange={(e) => f({ port: numParse(e.target.value) })} />
                  </FormField>
                  <FormField label="Encryption" className="md:col-span-2">
                    <Select value={encryption} onValueChange={setEncryption}>
                      <SelectTrigger aria-label="Encryption"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="ldaps">LDAPS (TLS, port 636)</SelectItem>
                        <SelectItem value="starttls">StartTLS (port 389)</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                </div>

                <FormField label="Directory CA certificate (PEM)" hint="Certificate verification is required. Leave blank to use the appliance trust store.">
                  <textarea className="min-h-24 w-full rounded border border-border bg-surface p-2 font-mono text-xs" value={form.ca_certificate_pem || ''} onChange={(e) => f({ ca_certificate_pem: e.target.value })} />
                </FormField>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="Bind DN" hint="Service account used to search the directory">
                    <Input value={form.bind_dn} onChange={(e) => f({ bind_dn: e.target.value })} placeholder="CN=svc-zenplus,OU=Service,DC=corp,DC=local" autoComplete="off" className="font-mono text-xs" />
                  </FormField>
                  <FormField label="Bind password" hint={clearSecret ? 'The stored password will be removed when you save.' : form.has_bind_password ? 'Saved. Leave blank to keep the stored password.' : undefined}>
                    <PasswordInput value={form.bind_password} onChange={(e) => { f({ bind_password: e.target.value }); setClearSecret(false) }} hasStored={form.has_bind_password && !clearSecret} />
                  </FormField>
                  {form.has_bind_password && (
                    <label className="flex cursor-pointer items-center gap-2 text-xs md:col-start-2">
                      <input type="checkbox" className="h-3.5 w-3.5 accent-primary" checked={clearSecret} onChange={(e) => { setClearSecret(e.target.checked); if (e.target.checked) f({ bind_password: '' }) }} />
                      Remove the stored bind password
                    </label>
                  )}
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
                  <p className="mt-2 text-xs text-muted">
                    Defaults suit Active Directory. For OpenLDAP use <code className="font-mono">(uid={'{username}'})</code> and the <code className="font-mono">cn</code> name attribute. Only direct group membership (<code className="font-mono">memberOf</code>) is read; nested groups are not expanded.
                  </p>
                </Advanced>
              </TabsContent>

              <TabsContent value="mapping" className="mt-3 space-y-3">
                {rolesFailed && <p role="alert" className="text-xs text-danger">The role list could not be loaded, so role mapping cannot be edited safely right now. Close this dialog and retry.</p>}
                <p className="text-xs text-muted">
                  Rules are evaluated <strong>top to bottom</strong> against the user's group memberships; the <strong>first match wins</strong>. Use the arrows to reorder.
                  A group can be given as a full DN (unambiguous) or just its CN, which can match groups with that name in any container.
                </p>
                <MappingRows mappings={form.group_mappings} setMappings={(m) => f({ group_mappings: m })} keyField="group" keyLabel="Directory group" keyPlaceholder="CN=NetAdmins,OU=Groups,DC=corp,DC=local" roles={roles} problems={problems} />
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="When no rule matches" hint="Deny keeps unmapped directory users out entirely. Choose a role only if every directory user should get it.">
                    <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone label="Role when no rule matches" />
                  </FormField>
                  <div className="flex flex-col justify-end">
                    <ToggleRow title="Create accounts on first sign-in" description="Off: only users who already have an LDAP account here can sign in." checked={form.auto_provision} onChange={(v) => f({ auto_provision: v })} />
                  </div>
                </div>
                <p className="text-xs text-muted">New accounts get no visibility tags (they see everything their role permits). Limit them afterwards in Users, or give the mapped role only the access a whole team should have.</p>
              </TabsContent>

              <TabsContent value="test" className="mt-3">
                <TestPanel kind="ldap" payload={() => payload() as Record<string, unknown>} roleLabel={roleLabel} />
              </TabsContent>
            </Tabs>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={saver.isPending || (submitted && !!blocked)}>
                {saver.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}

// ─── RADIUS dialog ──────────────────────────────────────────────────────────

type RadiusForm = Omit<RadiusConfig, 'port' | 'timeout' | 'retries'> & { port: number | ''; timeout: number | ''; retries: number | '' }

export function RadiusDialog({ open, onOpenChange, initial, roles, rolesFailed, initialTab, onReload }: {
  open: boolean; onOpenChange: (o: boolean) => void; initial: RadiusConfig; roles: Role[]; rolesFailed?: boolean
  initialTab: DialogTab; onReload: () => void
}) {
  const [form, setForm] = useState<RadiusForm>(initial)
  const [baseline, setBaseline] = useState('')
  const [tab, setTab] = useState<DialogTab>(initialTab)
  const [advanced, setAdvanced] = useState(false)
  const [clearSecret, setClearSecret] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    const f = { ...initial, secret: '' } as RadiusForm
    setForm(f); setBaseline(JSON.stringify(f))
    setTab(initialTab); setAdvanced(false); setClearSecret(false); setSubmitted(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const f = (patch: Partial<RadiusForm>) => setForm((prev) => ({ ...prev, ...patch }))
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])
  const timeout = form.timeout === '' ? 5 : Math.min(60, Math.max(1, form.timeout))
  const retries = form.retries === '' ? 3 : Math.min(10, Math.max(1, form.retries))
  const budget = timeout * retries
  const budgetBad = budget > 30
  const problems = validateMappings(form.class_mappings, 'value', 'reply value', true)

  const payload = (confirmPrivileged = false) => ({
    ...form,
    port: form.port === '' ? 1812 : form.port,
    timeout, retries,
    class_mappings: form.class_mappings.filter((m) => (m.value || '').trim() || m.role),
    expected_revision: initial.revision,
    clear_secret: clearSecret,
    confirm_privileged: confirmPrivileged,
  })
  const hasSecret = (!!form.secret || !!form.has_secret) && !clearSecret
  const canTest = !!form.server && hasSecret && !budgetBad
  const missing = form.enabled && (!form.server || !hasSecret)
  const dirty = open && JSON.stringify(form) !== baseline || clearSecret
  const saver = useProviderSave('radius', (c) => payload(c) as Record<string, unknown>, () => onOpenChange(false))
  const guard = useDirtyGuard(dirty, saver.isPending, onOpenChange)
  const blocked = problems.length > 0 || missing || budgetBad || (rolesFailed && form.class_mappings.length > 0)

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><Network className="h-4 w-4 text-primary" /> RADIUS</DialogTitle>
            <DialogDescription>PAP authentication against a RADIUS server. Nothing is applied until you save.</DialogDescription>
          </DialogHeader>

          <form noValidate onSubmit={(e) => { e.preventDefault(); setSubmitted(true); if (!blocked) saver.save() }} className="space-y-4">
            <SaveBanners s={saver} roleLabel={roleLabel} onConfirm={() => saver.save(true)} onReload={() => { saver.reset(); onOpenChange(false); onReload() }} />
            <Tabs value={tab} onValueChange={(v) => setTab(v as DialogTab)}>
              <DialogTabs showTest={canTest} mappingIssues={problems.length} />

              <TabsContent value="connection" className="mt-3 space-y-3">
                <ToggleRow title="Enable RADIUS sign-in" description="Tried after LDAP / Active Directory for unknown usernames." checked={form.enabled} onChange={(v) => f({ enabled: v })} />
                {missing && <p role="alert" className="text-xs text-warning">Server and shared secret are required before this source can be enabled.</p>}

                <div className="grid grid-cols-1 gap-3 md:grid-cols-6">
                  <FormField label="Server" required className="md:col-span-3">
                    <Input value={form.server} onChange={(e) => f({ server: e.target.value })} placeholder="radius.corp.local" />
                  </FormField>
                  <FormField label="Auth port">
                    <Input type="number" min={1} max={65535} value={numValue(form.port)} onChange={(e) => f({ port: numParse(e.target.value) })} />
                  </FormField>
                  <FormField label="Shared secret" required className="md:col-span-2" hint={clearSecret ? 'The stored secret will be removed when you save.' : form.has_secret ? 'Saved. Leave blank to keep the stored secret.' : undefined}>
                    <PasswordInput value={form.secret} onChange={(e) => { f({ secret: e.target.value }); setClearSecret(false) }} hasStored={form.has_secret && !clearSecret} />
                  </FormField>
                  {form.has_secret && (
                    <label className="flex cursor-pointer items-center gap-2 text-xs md:col-span-6">
                      <input type="checkbox" className="h-3.5 w-3.5 accent-primary" checked={clearSecret} onChange={(e) => { setClearSecret(e.target.checked); if (e.target.checked) f({ secret: '' }) }} />
                      Remove the stored shared secret (the source cannot be enabled without one)
                    </label>
                  )}
                </div>

                <Advanced open={advanced} onToggle={() => setAdvanced((v) => !v)}>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                    <FormField label="Timeout (seconds)" hint="1–60 per attempt">
                      <Input type="number" min={1} max={60} value={numValue(form.timeout)} onChange={(e) => f({ timeout: numParse(e.target.value) })} />
                    </FormField>
                    <FormField label="Retries" hint="1–10">
                      <Input type="number" min={1} max={10} value={numValue(form.retries)} onChange={(e) => f({ retries: numParse(e.target.value) })} />
                    </FormField>
                    <FormField label="NAS-Identifier" hint="Sent in every Access-Request">
                      <Input value={form.nas_identifier} onChange={(e) => f({ nas_identifier: e.target.value })} placeholder="zenplus" className="font-mono text-xs" />
                    </FormField>
                  </div>
                  <p className={cn('mt-2 text-xs', budgetBad ? 'text-danger' : 'text-muted')} role={budgetBad ? 'alert' : undefined}>
                    Worst-case wait: timeout × retries = {budget} s. Sign-in and the Test button give up after at most 30 s, so keep this at or below 30.
                  </p>
                  <p className="mt-1 text-xs text-muted">RADIUS here is PAP Access-Request/Accept/Reject only. Access-Challenge (token or push MFA served by the RADIUS server) is not supported; use ZenPlus's own two-step verification for local accounts.</p>
                </Advanced>
              </TabsContent>

              <TabsContent value="mapping" className="mt-3 space-y-3">
                {rolesFailed && <p role="alert" className="text-xs text-danger">The role list could not be loaded, so role mapping cannot be edited safely right now. Close this dialog and retry.</p>}
                <p className="text-xs text-muted">
                  Rules are matched against the reply attributes returned on Access-Accept, top to bottom; the <strong>first match wins</strong>.
                  Pick the attribute your RADIUS server uses for authorization (usually Class or Filter-Id). “Reply-Message” is human-readable text and is a poor privilege selector.
                </p>
                <MappingRows mappings={form.class_mappings} setMappings={(m) => f({ class_mappings: m })} keyField="value" keyLabel="Reply attribute value" keyPlaceholder="netadmins" roles={roles} withAttribute problems={problems} />
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="When no rule matches" hint="Deny keeps unmapped RADIUS users out entirely. Choose a role only if every RADIUS user should get it.">
                    <RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone label="Role when no rule matches" />
                  </FormField>
                  <div className="flex flex-col justify-end">
                    <ToggleRow title="Create accounts on first sign-in" description="Off: only users who already have a RADIUS account here can sign in." checked={form.auto_provision} onChange={(v) => f({ auto_provision: v })} />
                  </div>
                </div>
              </TabsContent>

              <TabsContent value="test" className="mt-3">
                <TestPanel kind="radius" payload={() => payload() as Record<string, unknown>} requireCreds roleLabel={roleLabel} />
              </TabsContent>
            </Tabs>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={saver.isPending || (submitted && !!blocked)}>
                {saver.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}


// ─── OpenID Connect dialog ──────────────────────────────────────────────────

type OidcTest = { success: boolean; message: string; redirect_uri?: string; warnings?: string[]; steps?: { name: string; ok: boolean; message?: string }[] }

function OidcTestPanel({ payload }: { payload: () => Record<string, unknown> }) {
  const [result, setResult] = useState<OidcTest | null>(null)
  const test = useMutation({
    mutationFn: async () => (await api.post('/system/auth/oidc/test', { oidc: payload() }, { timeout: 40_000 })).data as OidcTest,
    onSuccess: setResult,
    onError: (e: any) => setResult({ success: false, message: describeFailure(e).message }),
  })
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">Reads the provider's discovery document and signing keys using the values in this dialog (saved or not). A full sign-in needs a browser, so try it from the sign-in page after saving, ideally in a private window while keeping this session open.</p>
      <Button type="button" variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
        {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />} Check provider
      </Button>
      {result && (
        <div role="status" className={cn('rounded-md border p-3 text-sm', result.success ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5')}>
          <div className={cn('flex items-center gap-2 font-medium', result.success ? 'text-success' : 'text-danger')}>
            {result.success ? <CheckCircle2 className="h-4 w-4" aria-hidden /> : <XCircle className="h-4 w-4" aria-hidden />}{result.message}
          </div>
          {result.warnings && result.warnings.length > 0 && <ul className="mt-2 list-disc pl-5 text-xs text-warning">{result.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
          {result.redirect_uri && <p className="mt-2 text-xs text-muted">Register this redirect URI at the provider: <code className="select-all font-mono">{result.redirect_uri}</code></p>}
        </div>
      )}
    </div>
  )
}

export function OidcDialog({ open, onOpenChange, initial, roles, rolesFailed, initialTab, onReload }: {
  open: boolean; onOpenChange: (o: boolean) => void; initial: OidcConfig; roles: Role[]; rolesFailed?: boolean
  initialTab: DialogTab; onReload: () => void
}) {
  const [form, setForm] = useState<OidcConfig>(initial)
  const [baseline, setBaseline] = useState('')
  const [tab, setTab] = useState<DialogTab>(initialTab)
  const [advanced, setAdvanced] = useState(false)
  const [clearSecret, setClearSecret] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    const f = { ...initial, client_secret: '' }
    setForm(f); setBaseline(JSON.stringify(f)); setTab(initialTab); setAdvanced(false); setClearSecret(false); setSubmitted(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const f = (patch: Partial<OidcConfig>) => setForm((prev) => ({ ...prev, ...patch }))
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])
  const problems = validateMappings(form.group_mappings, 'group', 'group', false)
  const hasSecret = (!!form.client_secret || !!form.has_client_secret) && !clearSecret
  const missing = form.enabled && (!form.issuer || !form.client_id || !hasSecret)
  const redirect = `${(form.redirect_base_url || window.location.origin).replace(/\/$/, '')}/api/v1/auth/oidc/callback`

  const payload = (confirmPrivileged = false) => ({
    ...form,
    group_mappings: form.group_mappings.filter((m) => (m.group || '').trim() || m.role),
    expected_revision: initial.revision, clear_secret: clearSecret, confirm_privileged: confirmPrivileged,
  })
  const dirty = open && JSON.stringify(form) !== baseline || clearSecret
  const saver = useProviderSave('oidc', (c) => payload(c) as Record<string, unknown>, () => onOpenChange(false))
  const guard = useDirtyGuard(dirty, saver.isPending, onOpenChange)
  const blocked = problems.length > 0 || missing || (rolesFailed && form.group_mappings.length > 0)

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4 text-primary" /> OpenID Connect single sign-on</DialogTitle>
            <DialogDescription>Sign in with Microsoft Entra ID, Okta, Keycloak, Google Workspace or any OIDC provider. Authorization-code flow with PKCE. Nothing is applied until you save.</DialogDescription>
          </DialogHeader>
          <form noValidate onSubmit={(e) => { e.preventDefault(); setSubmitted(true); if (!blocked) saver.save() }} className="space-y-4">
            <SaveBanners s={saver} roleLabel={roleLabel} onConfirm={() => saver.save(true)} onReload={() => { saver.reset(); onOpenChange(false); onReload() }} />
            <Tabs value={tab} onValueChange={(v) => setTab(v as DialogTab)}>
              <DialogTabs showTest mappingIssues={problems.length} />

              <TabsContent value="connection" className="mt-3 space-y-3">
                <ToggleRow title="Enable single sign-on" description="Adds a “Sign in with …” button to the sign-in page. Local accounts keep working." checked={form.enabled} onChange={(v) => f({ enabled: v })} />
                {missing && <p role="alert" className="text-xs text-warning">Issuer, client ID and client secret are required before this source can be enabled.</p>}
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="Issuer URL" required hint="e.g. https://login.microsoftonline.com/<tenant>/v2.0 — the discovery document is read from here">
                    <Input value={form.issuer} onChange={(e) => f({ issuer: e.target.value })} placeholder="https://idp.example.com/realms/corp" className="font-mono text-xs" />
                  </FormField>
                  <FormField label="Button label" hint="Shown on the sign-in page">
                    <Input value={form.display_name} maxLength={60} onChange={(e) => f({ display_name: e.target.value })} placeholder="Single sign-on" />
                  </FormField>
                  <FormField label="Client ID" required><Input value={form.client_id} onChange={(e) => f({ client_id: e.target.value })} autoComplete="off" className="font-mono text-xs" /></FormField>
                  <FormField label="Client secret" required hint={clearSecret ? 'The stored secret will be removed when you save.' : form.has_client_secret ? 'Saved. Leave blank to keep the stored secret.' : undefined}>
                    <PasswordInput value={form.client_secret} onChange={(e) => { f({ client_secret: e.target.value }); setClearSecret(false) }} hasStored={form.has_client_secret && !clearSecret} />
                  </FormField>
                  {form.has_client_secret && (
                    <label className="flex cursor-pointer items-center gap-2 text-xs md:col-start-2">
                      <input type="checkbox" className="h-3.5 w-3.5 accent-primary" checked={clearSecret} onChange={(e) => { setClearSecret(e.target.checked); if (e.target.checked) f({ client_secret: '' }) }} /> Remove the stored client secret
                    </label>
                  )}
                </div>
                <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
                  <div className="font-medium">Redirect URI to register at the provider</div>
                  <div className="mt-1 flex items-center gap-2"><code className="select-all break-all font-mono">{redirect}</code>
                    <Button type="button" size="icon" variant="ghost" className="h-6 w-6" aria-label="Copy redirect URI" onClick={async () => { (await copyText(redirect)) ? toast.success('Copied') : toast.error('Copy failed') }}><Copy className="h-3 w-3" /></Button></div>
                </div>
                <Advanced open={advanced} onToggle={() => setAdvanced((v) => !v)}>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <FormField label="Scopes"><Input value={form.scopes} onChange={(e) => f({ scopes: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Public base URL override" hint="Only if the appliance is reached under a different address than the browser's"><Input value={form.redirect_base_url} onChange={(e) => f({ redirect_base_url: e.target.value })} placeholder="https://zenplus.corp.example" className="font-mono text-xs" /></FormField>
                    <FormField label="Username claim"><Input value={form.username_claim} onChange={(e) => f({ username_claim: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Email claim"><Input value={form.email_claim} onChange={(e) => f({ email_claim: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Name claim"><Input value={form.name_claim} onChange={(e) => f({ name_claim: e.target.value })} className="font-mono text-xs" /></FormField>
                    <FormField label="Groups claim" hint="Entra ID: “groups” (object IDs) · Okta/Keycloak: configure a groups claim"><Input value={form.groups_claim} onChange={(e) => f({ groups_claim: e.target.value })} className="font-mono text-xs" /></FormField>
                  </div>
                  <FormField label="Provider CA certificate (PEM)" hint="Only for a private CA; blank uses the appliance trust store."><textarea className="mt-1 min-h-20 w-full rounded border border-border bg-surface p-2 font-mono text-xs" value={form.ca_certificate_pem || ''} onChange={(e) => f({ ca_certificate_pem: e.target.value })} /></FormField>
                  <p className="mt-2 text-xs text-muted">An account is matched by the provider's stable subject (<code>sub</code>), never by username or email, so a renamed or reassigned address cannot take over another account. A local account with the same username blocks the sign-in instead of being merged.</p>
                </Advanced>
              </TabsContent>

              <TabsContent value="mapping" className="mt-3 space-y-3">
                {rolesFailed && <p role="alert" className="text-xs text-danger">The role list could not be loaded, so role mapping cannot be edited safely right now.</p>}
                <p className="text-xs text-muted">Rules match the values in the groups claim (case-insensitively), top to bottom; the <strong>first match wins</strong>. Use the identifier your provider puts in the claim (Entra ID sends group object IDs).</p>
                <MappingRows mappings={form.group_mappings} setMappings={(m) => f({ group_mappings: m })} keyField="group" keyLabel="Group claim value" keyPlaceholder="NetAdmins" roles={roles} problems={problems} />
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <FormField label="When no rule matches" hint="Deny keeps unmapped users out entirely."><RoleSelect value={form.default_role} onChange={(v) => f({ default_role: v })} roles={roles} allowNone label="Role when no rule matches" /></FormField>
                  <div className="flex flex-col justify-end"><ToggleRow title="Create accounts on first sign-in" description="Off: only users already linked to this provider can sign in." checked={form.auto_provision} onChange={(v) => f({ auto_provision: v })} /></div>
                </div>
              </TabsContent>

              <TabsContent value="test" className="mt-3"><OidcTestPanel payload={() => payload() as Record<string, unknown>} /></TabsContent>
            </Tabs>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={saver.isPending || (submitted && !!blocked)}>{saver.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}
