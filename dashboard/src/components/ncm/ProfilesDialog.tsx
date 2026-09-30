import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, CircleSlash, KeyRound, Loader2, Pencil, Plus, Save, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { ConnectionAccessGuide } from './ConnectionAccessGuide'
import { errorDetail, ncmError } from './model'
import { Ago, EmptyState, SectionError } from './ui'

export type Profile = {
  id: string; name: string; description: string | null; protocol: string; port: number; username: string; is_default: boolean
  auth_method: 'password' | 'key'; has_password: boolean; has_enable: boolean; has_private_key: boolean; has_passphrase: boolean
  key_type: string | null; key_fingerprint: string | null; secret_rotated_at: string | null; rotation_days: number | null
  rotation_due: boolean; rotation_unknown: boolean; used_by: number
}
type UsageRow = { device_id: string; hostname: string; ip: string | null; enabled: boolean; schedule_enabled: boolean }

type Form = {
  name: string; description: string; username: string; port: string; auth_method: 'password' | 'key'
  password: string; enable_password: string; clear_enable_password: boolean; private_key: string; key_passphrase: string
  is_default: boolean; rotation_days: string
}
const EMPTY: Form = {
  name: '', description: '', username: '', port: '22', auth_method: 'password', password: '', enable_password: '',
  clear_enable_password: false, private_key: '', key_passphrase: '', is_default: false, rotation_days: '',
}

function formOf(p: Profile): Form {
  return {
    ...EMPTY, name: p.name, description: p.description || '', username: p.username, port: String(p.port || 22),
    auth_method: p.auth_method || 'password', is_default: p.is_default, rotation_days: p.rotation_days ? String(p.rotation_days) : '',
  }
}

function validate(f: Form, editing: Profile | null): Partial<Record<keyof Form, string>> {
  const e: Partial<Record<keyof Form, string>> = {}
  if (!f.name.trim()) e.name = 'Name is required.'
  if (!f.username.trim()) e.username = 'Username is required.'
  const port = Number(f.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) e.port = 'Port must be 1–65535.'
  if (f.rotation_days.trim()) {
    const d = Number(f.rotation_days)
    if (!Number.isInteger(d) || d < 1 || d > 3650) e.rotation_days = 'Enter 1–3650 days, or leave blank.'
  }
  if (f.auth_method === 'password' && !f.password && !(editing?.has_password)) e.password = 'A password is required for a new password profile.'
  if (f.auth_method === 'key' && !f.private_key.trim() && !(editing?.has_private_key)) e.private_key = 'A private key is required for SSH key authentication.'
  if (f.private_key.trim() && !/-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(f.private_key)) e.private_key = 'Paste the full private key including the BEGIN/END lines.'
  return e
}

function SavedIndicator({ saved, what }: { saved: boolean; what: string }) {
  return saved
    ? <Badge variant="success" title={`${what} is stored encrypted`}><CheckCircle2 className="h-3 w-3" aria-hidden /> Saved</Badge>
    : <Badge variant="outline" title={`No ${what.toLowerCase()} stored`}><CircleSlash className="h-3 w-3" aria-hidden /> Not saved</Badge>
}

function RotationState({ p }: { p: Profile }) {
  if (p.rotation_due) return <Badge variant="warning"><AlertTriangle className="h-3 w-3" aria-hidden /> Rotation overdue</Badge>
  if (p.rotation_unknown) return <Badge variant="outline">Rotation date unknown</Badge>
  if (p.secret_rotated_at) return <span className="text-muted"><Ago iso={p.secret_rotated_at} prefix="Rotated " /></span>
  return null
}

export function ProfilesDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const can = useCan()
  const canEdit = can('ncm.credentials')
  const qc = useQueryClient()

  const list = useQuery<{ data: Profile[] }>({
    queryKey: ['ncm', 'credentials'],
    queryFn: async () => (await api.get('/ncm/credentials')).data,
    enabled: open,
  })
  const profiles = list.data?.data || []

  const [editing, setEditing] = useState<Profile | null>(null)
  const [form, setForm] = useState<Form>(EMPTY)
  const [submitted, setSubmitted] = useState(false)
  const [deleting, setDeleting] = useState<Profile | null>(null)

  const startNew = () => { setEditing(null); setForm(EMPTY); setSubmitted(false) }
  const startEdit = (p: Profile) => { setEditing(p); setForm(formOf(p)); setSubmitted(false) }
  useEffect(() => { if (!open) { startNew(); setDeleting(null) } }, [open])

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }))
  const errors = validate(form, editing)
  const showErr = (k: keyof Form) => (submitted ? errors[k] : undefined)

  const save = useMutation({
    mutationFn: async () => {
      const key = form.auth_method === 'key'
      const body = {
        name: form.name.trim(),
        description: form.description.trim() || null,
        protocol: 'ssh',
        port: Number(form.port),
        username: form.username.trim(),
        auth_method: form.auth_method,
        password: !key && form.password ? form.password : undefined,
        enable_password: !form.clear_enable_password && form.enable_password ? form.enable_password : undefined,
        clear_enable_password: form.clear_enable_password,
        private_key: key && form.private_key.trim() ? form.private_key.trim() + '\n' : undefined,
        key_passphrase: key && form.private_key.trim() && form.key_passphrase ? form.key_passphrase : undefined,
        is_default: form.is_default,
        rotation_days: form.rotation_days.trim() ? Number(form.rotation_days) : null,
      }
      if (editing) return (await api.put(`/ncm/credentials/${editing.id}`, body)).data
      return (await api.post('/ncm/credentials', body)).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Profile updated' : 'Profile created', form.name.trim())
      qc.invalidateQueries({ queryKey: ['ncm'] })
      startNew()
    },
  })

  const submit = () => {
    setSubmitted(true)
    if (Object.keys(errors).length) return
    save.mutate()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Connection profiles</DialogTitle>
          <DialogDescription>
            CLI credentials the collector uses to log in (password or SSH key). Secrets are stored encrypted and are never shown again.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
          {/* List */}
          <section aria-labelledby="profiles-list" className="min-w-0 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <h3 id="profiles-list" className="text-xs font-semibold uppercase tracking-wider text-muted">Profiles</h3>
              {canEdit && editing && (
                <Button size="sm" variant="outline" onClick={startNew}><Plus className="h-3.5 w-3.5" /> New profile</Button>
              )}
            </div>
            {list.isLoading ? (
              <div className="flex items-center gap-2 py-6 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading profiles…</div>
            ) : list.isError ? (
              <SectionError what="Connection profiles" onRetry={() => list.refetch()} />
            ) : !profiles.length ? (
              <EmptyState>No connection profiles yet.{canEdit ? ' Create one to enrol devices for backup.' : ''}</EmptyState>
            ) : (
              <ul className="space-y-1.5">
                {profiles.map((p) => (
                  <li key={p.id} className={cn('flex items-start justify-between gap-2 rounded-md border px-3 py-2 text-xs',
                    editing?.id === p.id ? 'border-primary ring-1 ring-primary/40' : 'border-border')}>
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="break-words text-sm font-medium">{p.name}</span>
                        {p.is_default && <Badge variant="info">Default</Badge>}
                        <Badge variant="outline">
                          {p.auth_method === 'key' ? <><KeyRound className="h-3 w-3" aria-hidden /> SSH key{p.key_type ? ` ${p.key_type}` : ''}</> : 'Password'}
                        </Badge>
                      </div>
                      <div className="break-all font-mono text-muted">{p.username}@{p.port}</div>
                      {p.description && <div className="break-words text-muted">{p.description}</div>}
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="text-muted">Used by {p.used_by} device{p.used_by === 1 ? '' : 's'}</span>
                        <RotationState p={p} />
                      </div>
                    </div>
                    {canEdit && (
                      <div className="flex shrink-0 gap-0.5">
                        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={`Edit profile ${p.name}`} title="Edit" onClick={() => startEdit(p)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-8 w-8 text-danger" aria-label={`Delete profile ${p.name}`} title="Delete" onClick={() => setDeleting(p)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Editor */}
          <section aria-labelledby="profiles-editor" className="min-w-0 space-y-3">
            <h3 id="profiles-editor" className="text-xs font-semibold uppercase tracking-wider text-muted">
              {editing ? `Edit profile · ${editing.name}` : 'New profile'}
            </h3>
            {!canEdit ? (
              <p className="rounded-md border border-border bg-surface2 px-3 py-2 text-xs text-muted">
                You need the credentials permission to create or change connection profiles.
              </p>
            ) : (
              <form className="space-y-3" noValidate onSubmit={(e) => { e.preventDefault(); submit() }}>
                <FormField label="Name" required error={showErr('name')}>
                  <Input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="Core switches SSH" maxLength={120} />
                </FormField>
                <FormField label="Description">
                  <Input value={form.description} onChange={(e) => set('description', e.target.value)} placeholder="Optional" />
                </FormField>
                <div className="grid grid-cols-[minmax(0,1fr)_6rem] gap-2">
                  <FormField label="Username" required error={showErr('username')} className="min-w-0">
                    <Input value={form.username} onChange={(e) => set('username', e.target.value)} autoComplete="off" maxLength={120} />
                  </FormField>
                  <FormField label="Port" error={showErr('port')} className="min-w-0">
                    <Input type="number" inputMode="numeric" min={1} max={65535} value={form.port} onChange={(e) => set('port', e.target.value)} />
                  </FormField>
                </div>

                <div className="space-y-1.5">
                  <div id="auth-method-label" className="text-sm font-medium">Authentication</div>
                  <div role="group" aria-labelledby="auth-method-label" className="inline-flex rounded-md border border-border p-0.5">
                    {([['password', 'Password'], ['key', 'SSH key']] as const).map(([v, l]) => (
                      <button key={v} type="button" aria-pressed={form.auth_method === v} onClick={() => set('auth_method', v)}
                        className={cn('rounded px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                          form.auth_method === v ? 'bg-primary text-white' : 'text-muted hover:bg-surface2 hover:text-text')}>
                        {l}
                      </button>
                    ))}
                  </div>
                </div>

                {form.auth_method === 'password' ? (
                  <div className="space-y-1.5">
                    {editing && <div className="flex items-center gap-2 text-xs text-muted">Saved password: <SavedIndicator saved={editing.has_password} what="Password" /></div>}
                    <FormField label={editing ? 'Password (leave blank to keep the saved one)' : 'Password'} required={!editing?.has_password} error={showErr('password')}>
                      <PasswordInput value={form.password} onChange={(e) => set('password', e.target.value)} />
                    </FormField>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {editing?.has_private_key && (
                      <div className="rounded-md border border-border bg-surface2 px-3 py-2 text-xs">
                        <div className="flex flex-wrap items-center gap-2"><span className="text-muted">Saved key:</span> <SavedIndicator saved what="Private key" />
                          {editing.key_type && <span className="font-mono">{editing.key_type}</span>}</div>
                        {editing.key_fingerprint && <div className="mt-1 break-all font-mono text-muted">{editing.key_fingerprint}</div>}
                      </div>
                    )}
                    <FormField label={editing?.has_private_key ? 'Private key (leave blank to keep the saved one)' : 'Private key'}
                      required={!editing?.has_private_key} error={showErr('private_key')}
                      hint="OpenSSH or PEM format. Register the matching public key on the device for the backup account.">
                      <Textarea value={form.private_key} onChange={(e) => set('private_key', e.target.value)} rows={5} spellCheck={false}
                        autoComplete="off" className="font-mono text-xs" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
                    </FormField>
                    <FormField label="Key passphrase (optional)"
                      hint={editing?.has_private_key ? `Only used with a new key${editing.has_passphrase ? '; the saved key has a passphrase' : ''}.` : 'Leave blank if the key is not encrypted.'}>
                      <PasswordInput value={form.key_passphrase} onChange={(e) => set('key_passphrase', e.target.value)} disabled={!form.private_key.trim() && !!editing?.has_private_key} />
                    </FormField>
                  </div>
                )}

                <div className="space-y-1.5">
                  {editing && <div className="flex items-center gap-2 text-xs text-muted">Saved enable secret: <SavedIndicator saved={editing.has_enable} what="Enable secret" /></div>}
                  <FormField label={editing ? 'Enable secret (leave blank to keep)' : 'Enable secret (optional)'}
                    hint="Only needed when the account must elevate (for example Cisco enable).">
                    <PasswordInput value={form.enable_password} onChange={(e) => set('enable_password', e.target.value)} disabled={form.clear_enable_password} />
                  </FormField>
                  {editing?.has_enable && (
                    <label className="flex items-center gap-2 text-xs">
                      <input type="checkbox" className="h-4 w-4 accent-primary" checked={form.clear_enable_password}
                        onChange={(e) => setForm((f) => ({ ...f, clear_enable_password: e.target.checked, enable_password: e.target.checked ? '' : f.enable_password }))} />
                      Clear the saved enable secret
                    </label>
                  )}
                </div>

                <FormField label="Rotation policy (days)" error={showErr('rotation_days')}
                  hint="Optional. The profile is flagged “Rotation overdue” when the password or key is older than this. Blank = no policy.">
                  <Input type="number" inputMode="numeric" min={1} max={3650} value={form.rotation_days} onChange={(e) => set('rotation_days', e.target.value)} placeholder="e.g. 90" />
                </FormField>

                <div className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                  <div>
                    <div className="text-sm font-medium" id="default-profile-label">Default profile</div>
                    <div className="text-[11px] text-muted">Pre-selected when enrolling devices. Only one profile can be the default.</div>
                  </div>
                  <Switch checked={form.is_default} onCheckedChange={(v) => set('is_default', v)} aria-label="Default profile" />
                </div>

                <details className="rounded-md border border-border px-3 py-2 text-xs">
                  <summary className="cursor-pointer select-none font-medium">Vendor access guidance</summary>
                  <div className="mt-2"><ConnectionAccessGuide /></div>
                </details>

                {save.isError && <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">{ncmError(save.error)}</p>}
                {submitted && Object.keys(errors).length > 0 && (
                  <p role="alert" className="text-xs text-danger">Fix the highlighted fields before saving.</p>
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  {editing && <Button type="button" variant="outline" onClick={startNew}>Cancel</Button>}
                  <Button type="submit" disabled={save.isPending}>
                    {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : editing ? <Save className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
                    {editing ? 'Save changes' : 'Create profile'}
                  </Button>
                </div>
              </form>
            )}
          </section>
        </div>

        <DeleteProfileDialog profile={deleting} profiles={profiles} onClose={() => setDeleting(null)}
          onDeleted={(id) => { if (editing?.id === id) startNew() }} />
      </DialogContent>
    </Dialog>
  )
}

function DeleteProfileDialog({ profile, profiles, onClose, onDeleted }: {
  profile: Profile | null; profiles: Profile[]; onClose: () => void; onDeleted: (id: string) => void
}) {
  const qc = useQueryClient()
  const [replacement, setReplacement] = useState('')
  const [conflict, setConflict] = useState<{ message: string; devices: UsageRow[] } | null>(null)
  useEffect(() => { setReplacement(''); setConflict(null) }, [profile?.id])

  const usage = useQuery<{ data: UsageRow[] }>({
    queryKey: ['ncm', 'credentials', profile?.id, 'usage'],
    queryFn: async () => (await api.get(`/ncm/credentials/${profile!.id}/usage`)).data,
    enabled: !!profile,
  })
  const devices = conflict?.devices || usage.data?.data || []
  const used = devices.length > 0
  const others = profiles.filter((p) => p.id !== profile?.id)

  const del = useMutation({
    mutationFn: async () => (await api.delete(`/ncm/credentials/${profile!.id}`, { params: used && replacement ? { replacement_id: replacement } : {} })).data,
    onSuccess: (d: any) => {
      const target = others.find((p) => p.id === replacement)
      toast.success('Profile deleted', d?.reassigned ? `${d.reassigned} device${d.reassigned === 1 ? '' : 's'} reassigned to ${target?.name || 'the replacement'}` : profile?.name)
      qc.invalidateQueries({ queryKey: ['ncm'] })
      onDeleted(profile!.id)
      onClose()
    },
    onError: (e: any) => {
      const det = errorDetail(e)
      if (e?.response?.status === 409 && det?.code === 'profile_in_use') setConflict({ message: det.message, devices: det.devices || [] })
    },
  })

  return (
    <Dialog open={!!profile} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><AlertTriangle className="h-5 w-5 text-danger" aria-hidden /> Delete profile “{profile?.name}”</DialogTitle>
          <DialogDescription>The saved credentials are destroyed and cannot be recovered.</DialogDescription>
        </DialogHeader>

        {usage.isLoading && !conflict ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Checking which devices use this profile…</div>
        ) : usage.isError && !conflict ? (
          <SectionError what="Profile usage" onRetry={() => usage.refetch()} />
        ) : used ? (
          <div className="min-w-0 space-y-3 text-sm">
            <p role={conflict ? 'alert' : undefined} className="text-warning">
              {conflict?.message || `This profile is used by ${devices.length} device${devices.length === 1 ? '' : 's'}. Choose a replacement profile; they are reassigned before the profile is deleted.`}
            </p>
            <ul className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border p-2 text-xs" aria-label="Affected devices">
              {devices.map((d) => (
                <li key={d.device_id} className="flex flex-wrap items-center justify-between gap-2">
                  <span className="min-w-0 break-all"><span className="font-medium">{d.hostname}</span>{d.ip && <span className="text-muted"> · {d.ip}</span>}</span>
                  <span className="flex gap-1">
                    <Badge variant={d.enabled ? 'success' : 'outline'}>{d.enabled ? 'Enabled' : 'Disabled'}</Badge>
                    <Badge variant={d.schedule_enabled ? 'info' : 'outline'}>{d.schedule_enabled ? 'Scheduled' : 'Not scheduled'}</Badge>
                  </span>
                </li>
              ))}
            </ul>
            {others.length ? (
              <FormField label="Replacement profile" required>
                <Select value={replacement} onValueChange={setReplacement}>
                  <SelectTrigger aria-label="Replacement profile"><SelectValue placeholder="Choose a profile" /></SelectTrigger>
                  <SelectContent>
                    {others.map((p) => <SelectItem key={p.id} value={p.id}>{p.name} ({p.username}@{p.port})</SelectItem>)}
                  </SelectContent>
                </Select>
              </FormField>
            ) : (
              <p className="text-xs text-muted">Create another profile first so these devices can be reassigned.</p>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted">No devices use this profile.</p>
        )}

        {del.isError && !conflict && <p role="alert" className="text-xs text-danger">{ncmError(del.error)}</p>}

        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={onClose} disabled={del.isPending}>Cancel</Button>
          <Button variant="destructive" onClick={() => del.mutate()}
            disabled={del.isPending || (usage.isLoading && !conflict) || (usage.isError && !conflict) || (used && !replacement)}>
            {del.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {used ? `Reassign ${devices.length} device${devices.length === 1 ? '' : 's'} and delete` : 'Delete profile'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
