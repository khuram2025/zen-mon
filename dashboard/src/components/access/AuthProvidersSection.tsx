import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, CheckCircle2, FlaskConical, HardDrive, History, KeyRound, Loader2, Network, Server, Settings2, ShieldCheck, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { describeFailure } from '@/lib/access'
import type { Role } from '@/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Button } from '@/components/ui/Button'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { StatusDot } from '@/components/ui/StatusDot'
import { Skeleton } from '@/components/ui/Skeleton'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { QueryBoundary, QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import { LdapDialog, OidcDialog, RadiusDialog, ldapConfigured, oidcConfigured, radiusConfigured, type DialogTab, type LdapConfig, type OidcConfig, type ProviderKind, type RadiusConfig } from './ProviderDialogs'

// Backend: server/app/api/v1/auth_settings.py (/api/v1/system/auth/*)

type LastTest = { at: string; ok: boolean; message: string; server: string; by: string } | null
type Counts = { accounts: number; active_accounts: number; open_sessions: number }
type Providers = { ldap: LdapConfig & Counts & { last_test: LastTest }; radius: RadiusConfig & Counts & { last_test: LastTest }; oidc: OidcConfig & Counts & { last_test: LastTest } }
type Policy = {
  mfa_required: 'off' | 'admins' | 'all_local'; lockout_threshold: number; lockout_minutes: number; revision: number
  coverage: { admins: number; admins_with_mfa: number; local_accounts: number; local_with_mfa: number }
}

const PROVIDER_LABEL: Record<ProviderKind, string> = { ldap: 'LDAP / Active Directory', radius: 'RADIUS', oidc: 'OpenID Connect' }

/** Settings → Users & Access → Authentication.
 *
 *  Appliance-style source list (local accounts plus LDAP/AD and RADIUS) with
 *  status, the result of the last connection test, and the role-assignment
 *  summary; a sign-in policy card (multi-factor requirement, lockout); and a
 *  change history. Editing happens in dialogs so the page stays an overview. */
export function AuthProvidersSection() {
  const qc = useQueryClient()
  const providersQ = useQuery<Providers>({ queryKey: ['auth', 'providers'], queryFn: async () => (await api.get('/system/auth/providers')).data })
  const rolesQ = useQuery<Role[]>({ queryKey: ['roles'], queryFn: async () => (await api.get('/roles')).data })
  const [editing, setEditing] = useState<ProviderKind | null>(null)
  const [editingTab, setEditingTab] = useState<DialogTab>('connection')
  const [toggleTarget, setToggleTarget] = useState<{ kind: ProviderKind; enabled: boolean } | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)

  const roles = rolesQ.data || []
  const roleLabel = useMemo(() => Object.fromEntries(roles.map((r) => [r.name, r.display_name])), [roles])

  function open(kind: ProviderKind, tab: DialogTab = 'connection') { setEditingTab(tab); setEditing(kind) }
  function reload() { void qc.invalidateQueries({ queryKey: ['auth'] }) }

  function onToggle(kind: ProviderKind, enabled: boolean) {
    const data = providersQ.data
    if (!data) return
    const configured = kind === 'ldap' ? ldapConfigured(data.ldap) : kind === 'radius' ? radiusConfigured(data.radius) : oidcConfigured(data.oidc)
    if (enabled && !configured) {
      toast.info('Configure the server first', 'Enter the connection details, then enable the source.')
      open(kind)
      return
    }
    setToggleTarget({ kind, enabled })
  }

  return (
    <div className="space-y-3">
      <QueryBoundary query={providersQ} what="authentication sources" skeleton={<div className="space-y-3"><Skeleton className="h-9 w-full" /><Skeleton className="h-40 w-full" /></div>}>
        {(data) => {
          const { ldap, radius, oidc } = data
          const externalEnabled = ldap.enabled || radius.enabled || oidc.enabled
          const counts = { local: 0, ldap: ldap.accounts, radius: radius.accounts }
          return (
            <>
              {/* Sign-in order strip */}
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface2/40 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-semibold uppercase tracking-wider text-muted">Sign-in order</span>
                  <OrderStep label="Local accounts" active />
                  <ArrowRight className="h-3 w-3 text-muted" aria-hidden />
                  <OrderStep label="LDAP / AD" active={ldap.enabled} />
                  <ArrowRight className="h-3 w-3 text-muted" aria-hidden />
                  <OrderStep label="RADIUS" active={radius.enabled} />
                  <span className="mx-1 text-muted">·</span>
                  <OrderStep label="Single sign-on (button)" active={oidc.enabled} />
                </div>
                <p className="text-[11px] text-muted">
                  {externalEnabled
                    ? 'Unknown usernames are checked against the enabled external sources in this order and given the mapped role. Sessions of directory users last at most an hour, after which the directory is consulted again.'
                    : 'Only local accounts can sign in. Configure and enable a source below to allow directory users.'}
                </p>
              </div>
              {rolesQ.isError && <QueryFailure compact error={rolesQ.error} what="roles (needed for role mapping)" onRetry={() => void rolesQ.refetch()} retrying={rolesQ.isFetching} />}

              <Card>
                <CardContent className="p-0">
                  <Table>
                    <caption className="sr-only">Sign-in sources</caption>
                    <THead className="bg-surface2/50">
                      <Tr>
                        <Th>Source</Th><Th>Status</Th><Th>Server</Th><Th>Role assignment</Th>
                        <Th className="text-right">Accounts</Th><Th className="w-[230px] text-right">Actions</Th>
                      </Tr>
                    </THead>
                    <TBody>
                      <Tr>
                        <Td><SourceCell icon={HardDrive} title="Local accounts" subtitle="Built-in · passwords stored on this appliance" /></Td>
                        <Td><StatusCell status="up" label="Always on" /></Td>
                        <Td className="text-xs text-muted">This appliance</Td>
                        <Td className="text-xs text-muted">Assigned per user</Td>
                        <Td className="text-right text-sm tabular-nums">{counts.local || '—'}</Td>
                        <Td />
                      </Tr>
                      <ProviderRow
                        icon={Server} title="LDAP / Active Directory" subtitle="Service-account search, then bind as the user"
                        cfg={ldap} configured={ldapConfigured(ldap)} unit="group" mappings={ldap.group_mappings.filter((m) => m.group && m.role).length}
                        roleLabel={roleLabel} busy={false}
                        serverText={ldap.server ? `${ldap.server}:${ldap.port}` : ''}
                        serverSub={ldap.server ? `${ldap.use_ssl ? 'LDAPS' : ldap.use_starttls ? 'StartTLS' : 'No encryption'}${ldap.base_dn ? ` · ${ldap.base_dn}` : ''}` : ''}
                        onTest={() => open('ldap', 'test')} onConfigure={() => open('ldap')} onToggle={(v) => onToggle('ldap', v)}
                      />
                      <ProviderRow
                        icon={Network} title="RADIUS" subtitle="PAP Access-Request · Class / Filter-Id reply attributes"
                        cfg={radius} configured={radiusConfigured(radius)} unit="attribute" mappings={radius.class_mappings.filter((m) => m.value && m.role).length}
                        roleLabel={roleLabel} busy={false}
                        serverText={radius.server ? `${radius.server}:${radius.port}` : ''}
                        serverSub={radius.server ? `Timeout ${radius.timeout}s × ${radius.retries} ${radius.retries === 1 ? 'try' : 'tries'}${radius.has_secret ? '' : ' · no shared secret'}` : ''}
                        onTest={() => open('radius', 'test')} onConfigure={() => open('radius')} onToggle={(v) => onToggle('radius', v)}
                      />
                      <ProviderRow
                        icon={KeyRound} title="OpenID Connect (SSO)" subtitle="Browser redirect · Entra ID, Okta, Keycloak, Google"
                        cfg={oidc} configured={oidcConfigured(oidc)} unit="group" mappings={oidc.group_mappings.filter((m) => m.group && m.role).length}
                        roleLabel={roleLabel} busy={false}
                        serverText={oidc.issuer ? oidc.issuer.replace(/^https?:\/\//, '') : ''}
                        serverSub={oidc.issuer ? `Client ${oidc.client_id || '—'}${oidc.has_client_secret ? '' : ' · no client secret'}` : ''}
                        onTest={() => open('oidc', 'test')} onConfigure={() => open('oidc')} onToggle={(v) => onToggle('oidc', v)}
                      />
                    </TBody>
                  </Table>
                </CardContent>
              </Card>

              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-[11px] text-muted">
                  Directory users are matched to a role on every sign-in through the source's mapping rules. Local accounts are never
                  affected by external sources, and an account created by one source cannot sign in through another.
                </p>
                <Button variant="ghost" size="sm" onClick={() => setHistoryOpen(true)}><History className="h-3.5 w-3.5" /> Change history</Button>
              </div>

              <LdapDialog open={editing === 'ldap'} onOpenChange={(o) => !o && setEditing(null)} initial={ldap} roles={roles} rolesFailed={rolesQ.isError && !rolesQ.data} initialTab={editingTab} onReload={reload} />
              <RadiusDialog open={editing === 'radius'} onOpenChange={(o) => !o && setEditing(null)} initial={radius} roles={roles} rolesFailed={rolesQ.isError && !rolesQ.data} initialTab={editingTab} onReload={reload} />
              <OidcDialog open={editing === 'oidc'} onOpenChange={(o) => !o && setEditing(null)} initial={oidc} roles={roles} rolesFailed={rolesQ.isError && !rolesQ.data} initialTab={editingTab} onReload={reload} />
              <ToggleDialog target={toggleTarget} data={data} onClose={() => setToggleTarget(null)} />
            </>
          )
        }}
      </QueryBoundary>

      <SignInPolicyCard />
      <HistoryDialog open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </div>
  )
}

// ─── List cells ─────────────────────────────────────────────────────────────

function OrderStep({ label, active }: { label: string; active: boolean }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 rounded border px-2 py-0.5', active ? 'border-primary/30 bg-primary/10 text-text' : 'border-border text-muted opacity-70')}>
      <StatusDot status={active ? 'up' : 'idle'} />
      {label}<span className="sr-only">{active ? ' (enabled)' : ' (not enabled)'}</span>
    </span>
  )
}

function SourceCell({ icon: Icon, title, subtitle }: { icon: typeof Server; title: string; subtitle: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface2/60"><Icon className="h-4 w-4 text-primary" aria-hidden /></div>
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        <div className="truncate text-[11px] text-muted">{subtitle}</div>
      </div>
    </div>
  )
}

function StatusCell({ status, label, muted }: { status: 'up' | 'idle' | 'warn' | 'down'; label: string; muted?: boolean }) {
  return <span className={cn('inline-flex items-center gap-2 text-sm', muted && 'text-muted')}><StatusDot status={status} /> {label}</span>
}

function ProviderRow({ icon, title, subtitle, cfg, configured, unit, mappings, roleLabel, serverText, serverSub, onTest, onConfigure, onToggle }: {
  icon: typeof Server; title: string; subtitle: string
  cfg: (LdapConfig | RadiusConfig | OidcConfig) & Counts & { last_test: LastTest }
  configured: boolean; unit: string; mappings: number; roleLabel: Record<string, string>; busy: boolean
  serverText: string; serverSub: string
  onTest: () => void; onConfigure: () => void; onToggle: (v: boolean) => void
}) {
  const defaultRole = cfg.default_role ? roleLabel[cfg.default_role] || cfg.default_role : null
  const lt = cfg.last_test
  return (
    <Tr>
      <Td><SourceCell icon={icon} title={title} subtitle={subtitle} /></Td>
      <Td>
        {cfg.enabled ? <StatusCell status="up" label="Enabled" /> : configured ? <StatusCell status="idle" label="Disabled" /> : <StatusCell status="idle" label="Not configured" muted />}
        {configured && (
          <div className="mt-0.5 text-[11px] text-muted">
            {lt
              ? <span className={lt.ok ? 'text-success' : 'text-danger'} title={`${lt.message} — ${lt.by}`}>{lt.ok ? <CheckCircle2 className="mr-0.5 inline h-3 w-3" aria-hidden /> : <XCircle className="mr-0.5 inline h-3 w-3" aria-hidden />}Last test {lt.ok ? 'passed' : 'failed'} {relativeTime(lt.at)}</span>
              : 'Never tested'}
          </div>
        )}
      </Td>
      <Td>
        {serverText ? (
          <div className="text-sm">
            <span className="font-mono text-xs">{serverText}</span>
            <div className="text-[11px] text-muted">{serverSub}</div>
          </div>
        ) : <span className="text-xs text-muted">—</span>}
      </Td>
      <Td>
        {!configured ? <span className="text-xs text-muted">—</span> : (
          <div className="text-xs">
            <div>
              {mappings > 0 ? <>{mappings} {unit} {mappings === 1 ? 'rule' : 'rules'}</> : <span className="text-muted">No {unit} rules</span>}
              <span className="text-muted"> · default </span>
              {defaultRole ? <span className="font-medium">{defaultRole}</span> : <span className="text-warning">deny</span>}
            </div>
            <div className="text-[11px] text-muted">{cfg.auto_provision ? 'Accounts created on first sign-in' : 'Existing accounts only'}</div>
          </div>
        )}
      </Td>
      <Td className="text-right text-sm tabular-nums">{cfg.accounts}</Td>
      <Td>
        <div className="flex items-center justify-end gap-1.5">
          <Button variant="ghost" size="sm" disabled={!configured} onClick={onTest} title={configured ? 'Test connection' : 'Configure the server first'}><FlaskConical className="h-3.5 w-3.5" /> Test</Button>
          <Button variant="outline" size="sm" onClick={onConfigure}><Settings2 className="h-3.5 w-3.5" /> Configure</Button>
          <Switch checked={cfg.enabled} onCheckedChange={(v) => onToggle(!!v)} aria-label={`${cfg.enabled ? 'Disable' : 'Enable'} ${title}`} />
        </div>
      </Td>
    </Tr>
  )
}

/** Enable / disable confirmation. Uses the narrow endpoint, so nothing else in
 *  the configuration can be touched by a stale browser, and states what
 *  disabling does to the accounts and sessions that rely on the source. */
function ToggleDialog({ target, data, onClose }: { target: { kind: ProviderKind; enabled: boolean } | null; data: Providers; onClose: () => void }) {
  const qc = useQueryClient()
  const [revoke, setRevoke] = useState(false)
  const cfg = target ? data[target.kind] : null
  const toggle = useMutation({
    mutationFn: async () => (await api.post(`/system/auth/${target!.kind}/enabled`, { enabled: target!.enabled, expected_revision: cfg?.revision, revoke_sessions: target!.enabled ? false : revoke })).data,
    onSuccess: (d) => {
      toast.success(`${PROVIDER_LABEL[target!.kind]} ${target!.enabled ? 'enabled' : 'disabled'}`, d.sessions_revoked ? `${d.sessions_revoked} session(s) signed out.` : undefined)
      qc.invalidateQueries({ queryKey: ['auth'] })
      setRevoke(false)
      onClose()
    },
    onError: (e: any) => {
      const f = describeFailure(e)
      toast.error('Update failed', f.message)
      if (f.code === 'provider_revision_conflict') { qc.invalidateQueries({ queryKey: ['auth'] }); onClose() }
    },
  })
  return (
    <Dialog open={!!target} onOpenChange={(o) => !o && !toggle.isPending && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{target?.enabled ? 'Enable' : 'Disable'} {target ? PROVIDER_LABEL[target.kind] : ''}</DialogTitle>
          <DialogDescription>
            {target?.enabled
              ? 'Unknown usernames will be looked up here when they sign in. Only the enabled flag changes; the rest of the configuration is untouched.'
              : 'Directory users will no longer be able to start a new sign-in through this source. Local accounts are unaffected.'}
          </DialogDescription>
        </DialogHeader>
        {cfg && !target?.enabled && (
          <div className="space-y-2 text-sm">
            <p className="text-xs">{cfg.accounts} account{cfg.accounts === 1 ? '' : 's'} ({cfg.active_accounts} active) sign in through this source; <strong>{cfg.open_sessions}</strong> {cfg.open_sessions === 1 ? 'session is' : 'sessions are'} open now.</p>
            <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2">
              <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-primary" checked={revoke} onChange={(e) => setRevoke(e.target.checked)} />
              <span className="text-xs"><span className="font-medium">Also sign those accounts out now</span><br /><span className="text-muted">Otherwise open sessions continue until they expire (within about an hour).</span></span>
            </label>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={toggle.isPending}>Cancel</Button>
          <Button variant={target?.enabled ? 'default' : 'destructive'} onClick={() => toggle.mutate()} disabled={toggle.isPending}>
            {toggle.isPending && <Loader2 className="h-4 w-4 animate-spin" />}{target?.enabled ? 'Enable' : 'Disable'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ─── Sign-in policy ─────────────────────────────────────────────────────────

function SignInPolicyCard() {
  const qc = useQueryClient()
  const q = useQuery<Policy>({ queryKey: ['auth', 'policy'], queryFn: async () => (await api.get('/system/auth/policy')).data })
  const [draft, setDraft] = useState<Pick<Policy, 'mfa_required' | 'lockout_threshold' | 'lockout_minutes'> | null>(null)
  const save = useMutation({
    mutationFn: async () => (await api.put('/system/auth/policy', draft)).data,
    onSuccess: () => { toast.success('Sign-in policy saved'); setDraft(null); qc.invalidateQueries({ queryKey: ['auth', 'policy'] }) },
    onError: (e: any) => {
      const f = describeFailure(e)
      toast.error(f.code === 'enable_own_mfa_first' ? 'Enable your own two-step verification first' : 'Save failed', f.code === 'enable_own_mfa_first' ? 'Open Settings → Profile → Two-step verification, then come back.' : f.message)
    },
  })
  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2 text-base"><ShieldCheck className="h-4 w-4 text-primary" /> Sign-in policy</CardTitle></CardHeader>
      <CardContent>
        <QueryBoundary query={q} what="the sign-in policy" skeleton={<Skeleton className="h-20 w-full" />}>
          {(p) => {
            const v = draft ?? { mfa_required: p.mfa_required, lockout_threshold: p.lockout_threshold, lockout_minutes: p.lockout_minutes }
            const dirty = !!draft && (draft.mfa_required !== p.mfa_required || draft.lockout_threshold !== p.lockout_threshold || draft.lockout_minutes !== p.lockout_minutes)
            const set = (patch: Partial<typeof v>) => setDraft({ ...v, ...patch })
            const cov = p.coverage
            return (
              <form onSubmit={(e) => { e.preventDefault(); if (dirty) save.mutate() }} className="space-y-3">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                  <FormField label="Require two-step verification" hint={`Local accounts only. ${cov.admins_with_mfa} of ${cov.admins} administrators and ${cov.local_with_mfa} of ${cov.local_accounts} local accounts have it today.`}>
                    <Select value={v.mfa_required} onValueChange={(x) => set({ mfa_required: x as Policy['mfa_required'] })}>
                      <SelectTrigger aria-label="Require two-step verification"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="off">Optional (users choose)</SelectItem>
                        <SelectItem value="admins">Required for administrators</SelectItem>
                        <SelectItem value="all_local">Required for all local accounts</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField label="Lock after failed sign-ins" hint="Consecutive failures before the account locks (3–100).">
                    <Input type="number" min={3} max={100} value={v.lockout_threshold} onChange={(e) => set({ lockout_threshold: Number(e.target.value) || 10 })} />
                  </FormField>
                  <FormField label="Lock duration (minutes)" hint="An administrator can unlock sooner from the Users list.">
                    <Input type="number" min={1} max={1440} value={v.lockout_minutes} onChange={(e) => set({ lockout_minutes: Number(e.target.value) || 15 })} />
                  </FormField>
                </div>
                {v.mfa_required !== 'off' && v.mfa_required !== p.mfa_required && (
                  <p role="note" className="text-xs text-warning">Accounts without two-step verification are blocked from everything except setting it up the next time they make a request. Make sure your own is enabled and you have recovery codes saved. Directory (LDAP/RADIUS) accounts rely on their provider's own MFA.</p>
                )}
                <div className="flex justify-end gap-2">
                  {dirty && <Button type="button" variant="outline" onClick={() => setDraft(null)}>Discard</Button>}
                  <Button type="submit" disabled={!dirty || save.isPending}>{save.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save policy</Button>
                </div>
              </form>
            )
          }}
        </QueryBoundary>
      </CardContent>
    </Card>
  )
}

// ─── Change history ─────────────────────────────────────────────────────────

type HistoryRow = { at: string; action: string; actor: string | null; provider: string; details: Record<string, any> }

function HistoryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const q = useQuery<{ data: HistoryRow[] }>({ queryKey: ['auth', 'history'], queryFn: async () => (await api.get('/system/auth/history')).data, enabled: open })
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Authentication change history</DialogTitle>
          <DialogDescription>Who changed which setting and when. Secrets and certificates are never recorded, only whether a secret was replaced or cleared.</DialogDescription>
        </DialogHeader>
        <QueryBoundary query={q} what="the change history" skeleton={<Skeleton className="h-32 w-full" />}>
          {(h) => h.data.length === 0 ? <p className="py-6 text-center text-sm text-muted">No recorded changes yet.</p> : (
            <ol className="space-y-2">
              {h.data.map((r, i) => {
                const changes = (r.details?.changes || (r.action === 'settings.auth.policy' ? r.details : null)) as Record<string, { from: unknown; to: unknown }> | null
                return (
                  <li key={i} className="rounded-md border border-border p-2.5 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-1">
                      <span className="font-medium">{r.provider || 'auth'} · {r.action.replace('settings.auth.', '')}{r.details?.secret && r.details.secret !== 'unchanged' ? ` · secret ${r.details.secret}` : ''}</span>
                      <span className="text-muted">{r.actor || 'system'} · <time dateTime={r.at}>{new Date(r.at).toLocaleString()}</time></span>
                    </div>
                    {r.details?.privileged_grants?.length > 0 && <div className="mt-1 text-warning">Privileged grants: {r.details.privileged_grants.join(', ')}</div>}
                    {r.action === 'settings.auth.toggle' && <div className="mt-1 text-muted">{r.details.enabled ? 'Enabled' : 'Disabled'}{r.details.sessions_revoked ? ` · ${r.details.sessions_revoked} sessions signed out` : ''}</div>}
                    {changes && Object.keys(changes).length > 0 && (
                      <ul className="mt-1.5 space-y-0.5 font-mono text-[11px]">
                        {Object.entries(changes).map(([k, c]) => (
                          <li key={k}><span className="text-muted">{k}:</span> <span className="text-danger">{JSON.stringify(c?.from)}</span> → <span className="text-success">{JSON.stringify(c?.to)}</span></li>
                        ))}
                      </ul>
                    )}
                  </li>
                )
              })}
            </ol>
          )}
        </QueryBoundary>
        <DialogFooter><Button variant="outline" onClick={onClose}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
void apiErrorMessage
