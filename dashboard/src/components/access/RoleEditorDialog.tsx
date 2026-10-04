import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { describeFailure, dependentsOf, isElevated, riskOf, withImplied, type Catalog } from '@/lib/access'
import type { PermissionCatalogModule, Role } from '@/types'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { useAuth, hasPermission } from '@/stores/auth'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'
import { PermissionMatrix, RiskBadge } from './PermissionMatrix'

const NAME_RE = /^[a-z0-9][a-z0-9_-]{1,49}$/
const RESERVED = new Set(['admin', 'administrator', 'owner', 'root', 'superuser', 'super_admin', 'system', 'operator', 'editor', 'viewer', 'read_only', 'readonly', 'guest', 'anonymous', 'service', 'agent', 'sensor'])

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 50)

type Impact = {
  added: string[]; removed: string[]; privileged_added: string[]; sensitive_added: string[]
  affected_users: number; affected_active: number
  sample_users: { username: string; is_active: boolean; auth_source: string; scope_tags: string[] }[]
  provider_references: { provider_label: string; kind: string; match: string; enabled: boolean }[]
  would_remove_last_administrator: boolean
  grants_beyond_you: string[]
}

/** Create / edit / duplicate a custom role.
 *
 *  - validates identifier and display name as you type (same rules as the API);
 *  - marks privileged permissions, adds the permissions a selection depends on,
 *    and locks permissions the signed-in account does not hold itself;
 *  - shows an impact review (who and what is affected) before a permission
 *    change is saved, and records an optional reason in the role history;
 *  - sends the revision it loaded so a stale save is refused, never silent;
 *  - asks before throwing away a changed draft. */
export function RoleEditorDialog({ open, onOpenChange, role, template, catalog }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  role: Role | null
  template: Role | null
  catalog?: Catalog
}) {
  const qc = useQueryClient()
  const me = useAuth((s) => s.user)
  const hydrate = useAuth((s) => s.hydrateUser)
  const isEdit = !!role
  const superPerm = catalog?.superuser_permission || 'system.admin'

  const [displayName, setDisplayName] = useState('')
  const [name, setName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [description, setDescription] = useState('')
  const [granted, setGranted] = useState<Set<string>>(new Set())
  const [baseline, setBaseline] = useState<Set<string>>(new Set())
  const [reason, setReason] = useState('')
  const [step, setStep] = useState<'edit' | 'review'>('edit')
  const [revision, setRevision] = useState<number | undefined>(undefined)
  const [serverErrors, setServerErrors] = useState<{ name?: string; display_name?: string; general?: string }>({})
  const [conflict, setConflict] = useState<Role | null>(null)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!open) return
    const src = role || template
    setDisplayName(role ? role.display_name : template ? `${template.display_name} (copy)` : '')
    setName(role ? role.name : '')
    setNameTouched(!!role)
    setDescription(src?.description || '')
    const perms = new Set(src?.permissions || [])
    setGranted(perms)
    setBaseline(role ? new Set(role.permissions) : new Set())
    setRevision(role?.revision)
    setReason('')
    setStep('edit')
    setServerErrors({})
    setConflict(null)
    setSubmitted(false)
  }, [open, role, template])

  const generated = useMemo(() => slugify(displayName), [displayName])
  const identifier = nameTouched ? name : generated
  const autoIdentifier = !nameTouched && generated.length < 2   // e.g. an Arabic display name

  const errors = {
    display_name:
      displayName.trim().length < 2 ? 'Enter a display name of at least 2 characters'
      : displayName.trim().length > 100 ? 'Display name can be at most 100 characters'
      : serverErrors.display_name,
    name:
      autoIdentifier ? undefined
      : !NAME_RE.test(identifier) ? 'Use 2–50 lowercase letters, digits, “_” or “-”, starting with a letter or digit'
      : RESERVED.has(identifier) ? `“${identifier}” is reserved; choose another identifier`
      : serverErrors.name,
    description: description.length > 2000 ? 'Description can be at most 2000 characters' : undefined,
  }
  const valid = !errors.display_name && !errors.name && !errors.description

  const added = useMemo(() => [...granted].filter((p) => !baseline.has(p)), [granted, baseline])
  const removed = useMemo(() => [...baseline].filter((p) => !granted.has(p)), [granted, baseline])
  const elevatedAdded = added.filter((p) => isElevated(riskOf(catalog, p)))
  const dirty = isEdit
    ? added.length > 0 || removed.length > 0 || displayName !== role!.display_name || description !== (role!.description || '') || identifier !== role!.name
    : displayName.trim() !== '' || description !== '' || granted.size > 0
  const permissionsChanged = added.length > 0 || removed.length > 0
  const needsReview = isEdit ? permissionsChanged : elevatedAdded.length > 0 || granted.has(superPerm)
  const full = granted.has(superPerm)

  const canGrant = (id: string) => hasPermission(me, id)

  function toggle(id: string, on: boolean) {
    setGranted((prev) => {
      if (on) return withImplied(catalog, prev, id)
      const next = new Set(prev)
      next.delete(id)
      // Removing a permission that others depend on would leave them dangling.
      const dependents = dependentsOf(catalog, prev, id)
      for (const d of dependents) next.delete(d)
      return next
    })
  }
  function toggleModule(m: PermissionCatalogModule, on: boolean) {
    setGranted((prev) => {
      let next = new Set(prev)
      for (const p of m.permissions) {
        if (isElevated(p.risk || 'standard') || !canGrant(p.id)) continue
        if (on) next = withImplied(catalog, next, p.id)
        else next.delete(p.id)
      }
      return next
    })
  }
  function selectStandard() {
    setGranted(() => {
      let next = new Set<string>()
      for (const m of catalog?.modules || []) for (const p of m.permissions) {
        if (!isElevated(p.risk || 'standard') && canGrant(p.id)) next = withImplied(catalog, next, p.id)
      }
      return next
    })
  }

  const impact = useQuery<Impact>({
    queryKey: ['roles', 'impact', role?.id, [...granted].sort().join(',')],
    queryFn: async () => (await api.post(`/roles/${role!.id}/impact`, { permissions: [...granted] })).data,
    enabled: open && isEdit && step === 'review',
    staleTime: 0,
  })

  const save = useMutation({
    mutationFn: async (overrideRevision?: number) => {
      const payload: Record<string, unknown> = {
        display_name: displayName.trim(),
        description: description.trim(),
        permissions: [...granted],
        reason: reason.trim() || undefined,
      }
      if (nameTouched || isEdit) { if (!autoIdentifier) payload.name = identifier }
      if (isEdit) {
        payload.expected_revision = overrideRevision ?? revision
        return (await api.put(`/roles/${role!.id}`, payload)).data
      }
      return (await api.post('/roles', payload)).data
    },
    onSuccess: (data: Role) => {
      toast.success(isEdit ? 'Role updated' : 'Role created', isEdit ? 'The change reaches every API worker within about 5 seconds; signed-in users stay signed in.' : undefined)
      qc.invalidateQueries({ queryKey: ['roles'] })
      qc.invalidateQueries({ queryKey: ['users'] })
      // The signed-in user's own role may just have changed.
      void hydrate()
      void data
      forceClose()
    },
    onError: (e: any) => {
      const f = describeFailure(e)
      if (f.kind === 'conflict' && f.code === 'role_revision_conflict') {
        setConflict(f.detail?.current as Role)
        setStep('edit')
        return
      }
      setStep('edit')
      const msg = f.message
      if (f.status === 409 && /identifier/i.test(msg)) setServerErrors({ name: msg })
      else if (f.status === 400 && /identifier|reserved/i.test(msg)) setServerErrors({ name: msg })
      else if (f.status === 422 && /display name/i.test(msg)) setServerErrors({ display_name: msg })
      else setServerErrors({ general: msg })
    },
  })

  const guard = useDirtyGuard(dirty && !save.isSuccess, save.isPending, onOpenChange)
  function forceClose() { onOpenChange(false) }

  function submit() {
    setSubmitted(true)
    setServerErrors({})
    if (!valid || !catalog) return
    if (needsReview && step === 'edit') { setStep('review'); return }
    save.mutate(undefined)
  }

  const title = isEdit ? `Edit role: ${role?.display_name}` : template ? `New role from ${template.display_name}` : 'New role'
  const copyOfAdmin = !isEdit && !!template && template.permissions.includes(superPerm)

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{step === 'review' ? 'Review before saving' : title}</DialogTitle>
            <DialogDescription>
              {step === 'review'
                ? 'Check who and what this change affects. Nothing is saved until you confirm.'
                : isEdit && role?.user_count
                  ? `${role.user_count} ${role.user_count === 1 ? 'user has' : 'users have'} this role. A saved change reaches every API worker within about 5 seconds; signed-in users keep their session.`
                  : 'A role is a named set of permissions you assign to users.'}
            </DialogDescription>
          </DialogHeader>

          {step === 'edit' && (
            <form noValidate onSubmit={(e) => { e.preventDefault(); submit() }} className="space-y-4">
              {conflict && (
                <div role="alert" className="space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
                  <div className="flex items-center gap-2 font-medium"><AlertTriangle className="h-4 w-4 text-warning" /> Someone else changed this role</div>
                  <p className="text-xs text-muted">Revision {conflict.revision} was saved{conflict.updated_by ? ` by ${conflict.updated_by}` : ''} after you opened it. Reload to review their version, or keep your edits and overwrite it.</p>
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" variant="outline" onClick={() => {
                      setDisplayName(conflict.display_name); setDescription(conflict.description || '')
                      setGranted(new Set(conflict.permissions)); setBaseline(new Set(conflict.permissions))
                      setName(conflict.name); setRevision(conflict.revision); setConflict(null)
                    }}><RefreshCw className="h-3.5 w-3.5" /> Reload latest (discard my edits)</Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => { setRevision(conflict.revision); setBaseline(new Set(conflict.permissions)); setConflict(null) }}>Keep my edits and overwrite</Button>
                  </div>
                </div>
              )}
              {serverErrors.general && <div role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">{serverErrors.general}</div>}
              {copyOfAdmin && (
                <div role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                  <span>You are copying <strong>Full administration</strong>. This copy would be just as powerful as the original, not a safe starting point for a limited role. Untick it below to build a narrower role.</span>
                </div>
              )}

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <FormField label="Display name" required error={(submitted || displayName) ? errors.display_name : undefined}>
                  <Input value={displayName} maxLength={120} onChange={(e) => { setDisplayName(e.target.value); setServerErrors({}) }} placeholder="Helpdesk" autoFocus={!isEdit} aria-invalid={!!errors.display_name} />
                </FormField>
                <FormField
                  label="Identifier"
                  error={(submitted || nameTouched) ? errors.name : undefined}
                  hint={autoIdentifier
                    ? 'No Latin letters in the display name, so an identifier like role_a1b2c3 is generated. Type your own to override.'
                    : isEdit ? 'Renaming carries assigned users and directory mappings along automatically.' : 'Stable internal name used by directory mappings and the audit log.'}
                >
                  <Input value={autoIdentifier ? '' : identifier} onChange={(e) => { setName(e.target.value.toLowerCase()); setNameTouched(true); setServerErrors({}) }} placeholder={autoIdentifier ? 'role_… (generated)' : 'helpdesk'} className="font-mono text-xs" aria-invalid={!!errors.name} />
                </FormField>
              </div>
              <FormField label="Description" error={errors.description} hint={`${description.length}/2000`}>
                <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this role is for…" />
              </FormField>

              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-semibold uppercase tracking-wider text-muted">
                    Permissions
                    <span className="ml-2 font-normal normal-case tracking-normal">
                      {full ? 'full administration' : `${granted.size} selected`}
                      {elevatedAdded.length > 0 && <span className="ml-2 text-warning">· {elevatedAdded.length} privileged added</span>}
                    </span>
                  </div>
                  {!full && (
                    <div className="flex gap-1">
                      <Button type="button" variant="ghost" size="sm" onClick={selectStandard} title="Selects every standard-risk permission you may grant. Sensitive and privileged permissions are never included; tick those individually.">Select all standard</Button>
                      <Button type="button" variant="ghost" size="sm" onClick={() => setGranted(new Set())}>Clear all</Button>
                    </div>
                  )}
                </div>
                <PermissionMatrix
                  catalog={catalog}
                  granted={granted}
                  superPerm={superPerm}
                  baseline={isEdit ? baseline : undefined}
                  canGrant={canGrant}
                  onToggle={toggle}
                  onToggleModule={toggleModule}
                  searchable
                />
                {full && (
                  <p className="flex items-center gap-1.5 text-[11px] text-warning">
                    <ShieldAlert className="h-3.5 w-3.5" /> Full administration grants every permission, including user, security, and system management, and ignores visibility scope.
                  </p>
                )}
              </div>

              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)}>Cancel</Button>
                <Button type="submit" disabled={save.isPending || !catalog || (submitted && !valid)}>
                  {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                  {needsReview ? 'Review changes' : isEdit ? 'Save changes' : 'Create role'}
                </Button>
              </DialogFooter>
            </form>
          )}

          {step === 'review' && (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <ChangeList title="Granting" tone="success" ids={added} catalog={catalog} empty="Nothing new" />
                <ChangeList title="Removing" tone="danger" ids={removed} catalog={catalog} empty="Nothing removed" />
              </div>
              {elevatedAdded.length > 0 && (
                <p role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                  This grants privileged access: {elevatedAdded.join(', ')}. Anyone with this role can change access or shared infrastructure.
                </p>
              )}
              {isEdit && (
                <div className="rounded-md border border-border p-3 text-sm">
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Who is affected</div>
                  {impact.isLoading && <div className="flex items-center gap-2 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Calculating impact…</div>}
                  {impact.isError && <p className="text-xs text-danger">Could not calculate the impact: {describeFailure(impact.error).message}. You can still save; the server enforces the rules.</p>}
                  {impact.data && (
                    <div className="space-y-1.5 text-xs">
                      <div><strong className="tabular-nums">{impact.data.affected_users}</strong> {impact.data.affected_users === 1 ? 'account has' : 'accounts have'} this role ({impact.data.affected_active} active, {impact.data.affected_users - impact.data.affected_active} disabled).</div>
                      {impact.data.sample_users.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {impact.data.sample_users.map((u) => (
                            <span key={u.username} className="rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-[10px]">{u.username}{!u.is_active ? ' (disabled)' : ''}{u.auth_source !== 'local' ? ` · ${u.auth_source}` : ''}</span>
                          ))}
                          {impact.data.affected_users > impact.data.sample_users.length && <span className="text-muted">+{impact.data.affected_users - impact.data.sample_users.length} more</span>}
                        </div>
                      )}
                      {impact.data.provider_references.length > 0
                        ? <div>Directory rules pointing at this role: {impact.data.provider_references.map((r) => `${r.provider_label} (${r.kind.replace('_', ' ')}${r.match ? `: ${r.match}` : ''})`).join('; ')}. Their members follow this change at next sign-in.</div>
                        : <div className="text-muted">No directory rule points at this role.</div>}
                      {impact.data.would_remove_last_administrator && <div role="alert" className="font-medium text-danger">This would leave no active full administrator; the server will refuse it.</div>}
                      {impact.data.grants_beyond_you.length > 0 && <div role="alert" className="font-medium text-danger">You cannot grant: {impact.data.grants_beyond_you.join(', ')}. The server will refuse it.</div>}
                    </div>
                  )}
                </div>
              )}
              <FormField label="Reason (optional, recorded in the role history)" hint={elevatedAdded.length > 0 ? 'Recommended for privileged changes.' : undefined}>
                <Textarea value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="e.g. Tier-2 helpdesk needs to reset passwords" />
              </FormField>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setStep('edit')} disabled={save.isPending}>Back</Button>
                <Button type="button" onClick={() => save.mutate(undefined)} disabled={save.isPending} variant={elevatedAdded.length ? 'destructive' : 'default'}>
                  {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                  {isEdit ? 'Confirm and save' : 'Confirm and create'}
                </Button>
              </DialogFooter>
            </div>
          )}
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}

function ChangeList({ title, ids, tone, catalog, empty }: { title: string; ids: string[]; tone: 'success' | 'danger'; catalog?: Catalog; empty: string }) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className={cn('mb-1 text-xs font-semibold uppercase tracking-wider', tone === 'success' ? 'text-success' : 'text-danger')}>{title} ({ids.length})</div>
      {ids.length === 0 ? <div className="text-xs text-muted">{empty}</div> : (
        <ul className="space-y-0.5 text-xs">
          {ids.map((id) => <li key={id} className="flex items-center gap-1.5 font-mono">{id}<RiskBadge risk={riskOf(catalog, id)} /></li>)}
        </ul>
      )}
    </div>
  )
}
