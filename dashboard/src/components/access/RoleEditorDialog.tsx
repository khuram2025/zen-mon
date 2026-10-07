import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, AppWindow, BookOpen, Check, ChevronLeft, ChevronRight, ClipboardCheck, Eye, Globe2, IdCard,
  KeyRound, Loader2, Network, PanelsTopLeft, Plus, RefreshCw, Server, ShieldAlert, ShieldCheck, Tags, Wrench,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { describeFailure, isElevated, riskOf, type Catalog } from '@/lib/access'
import type { Role } from '@/types'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { TagPicker } from '@/components/tags/TagPicker'
import { useTags } from '@/hooks/useTags'
import { useAuth, hasPermission } from '@/stores/auth'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'
import { RiskBadge } from './PermissionMatrix'
import { AccessLevelPicker } from './AccessLevelPicker'
import { MenuPreview, ScopeChips } from './MenuPreview'
import { cleanTags, fullLabel, groupByArea, levelsFor, moduleLevel, sameTags } from './roleModel'

const NAME_RE = /^[a-z0-9][a-z0-9_-]{1,49}$/
const RESERVED = new Set([
  'admin', 'administrator', 'owner', 'root', 'superuser', 'super_admin', 'system', 'operator', 'editor', 'viewer',
  'read_only', 'readonly', 'guest', 'anonymous', 'service', 'agent', 'sensor',
  'network_team', 'systems_team', 'application_team', 'security_team',
])

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 50)

type Impact = {
  added: string[]; removed: string[]; privileged_added: string[]; sensitive_added: string[]
  affected_users: number; affected_active: number
  sample_users: { username: string; is_active: boolean; auth_source: string; scope_tags: string[] }[]
  provider_references: { provider_label: string; kind: string; match: string; enabled: boolean }[]
  would_remove_last_administrator: boolean
  grants_beyond_you: string[]
}

type Step = 'details' | 'access' | 'visibility' | 'review'
const STEPS: { id: Step; label: string; hint: string; icon: typeof IdCard }[] = [
  { id: 'details', label: 'Details', hint: 'Name and starting point', icon: IdCard },
  { id: 'access', label: 'Access', hint: 'What members can open and change', icon: KeyRound },
  { id: 'visibility', label: 'Visibility', hint: 'Which resources they see', icon: Tags },
  { id: 'review', label: 'Review', hint: 'Check and save', icon: ClipboardCheck },
]

/** Built-in roles offered as starting points, team roles first. */
const TEMPLATE_ORDER = ['network_team', 'systems_team', 'application_team', 'security_team', 'operator', 'viewer', 'read_only']
const TEMPLATE_ICONS: Record<string, typeof Network> = {
  network_team: Network, systems_team: Server, application_team: AppWindow, security_team: ShieldCheck,
  operator: Wrench, viewer: Eye, read_only: BookOpen,
}

/** Create / edit / duplicate a custom role, in four steps.
 *
 *  - Details: name, identifier and a starting point (blank, a team role or
 *    another built-in), validated as you type with the API's rules;
 *  - Access: per-module presets (No access / View / Full) grouped by product
 *    area, sensitive and privileged permissions ticked individually, locked
 *    when the signed-in account does not hold them, with a live preview of
 *    the menu members will get;
 *  - Visibility: optionally limit every member to resources carrying tags;
 *  - Review: what changes, who is affected, an optional reason for the role
 *    history. The loaded revision is sent so a stale save is refused, and a
 *    changed draft asks before it is thrown away. */
export function RoleEditorDialog({ open, onOpenChange, role, template, catalog, roles }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  role: Role | null
  template: Role | null
  catalog?: Catalog
  /** Existing roles, for the "start from" choices. */
  roles?: Role[]
}) {
  const qc = useQueryClient()
  const me = useAuth((s) => s.user)
  const hydrate = useAuth((s) => s.hydrateUser)
  const isEdit = !!role
  const superPerm = catalog?.superuser_permission || 'system.admin'
  const modules = catalog?.modules || []

  const [step, setStep] = useState<Step>('details')
  const [displayName, setDisplayName] = useState('')
  const [name, setName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [description, setDescription] = useState('')
  const [granted, setGranted] = useState<Set<string>>(new Set())
  const [baseline, setBaseline] = useState<Set<string>>(new Set())
  const [scopeMode, setScopeMode] = useState<'all' | 'tags'>('all')
  const [scopeTags, setScopeTags] = useState<string[]>([])
  const [baselineScope, setBaselineScope] = useState<string[]>([])
  const [startFrom, setStartFrom] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [revision, setRevision] = useState<number | undefined>(undefined)
  const [serverErrors, setServerErrors] = useState<{ name?: string; display_name?: string; general?: string }>({})
  const [conflict, setConflict] = useState<Role | null>(null)
  const [submitted, setSubmitted] = useState(false)
  // The tag requirement is only reported once the user tries to move past Visibility.
  const [triedReview, setTriedReview] = useState(false)

  useEffect(() => {
    if (!open) return
    const src = role || template
    setStep('details')
    setDisplayName(role ? role.display_name : template ? `${template.display_name} (copy)` : '')
    setName(role ? role.name : '')
    setNameTouched(!!role)
    setDescription(src?.description || '')
    setGranted(new Set(src?.permissions || []))
    setBaseline(role ? new Set(role.permissions) : new Set())
    const tags = src?.scope_tags || []
    setScopeTags(tags)
    setScopeMode(tags.length ? 'tags' : 'all')
    setBaselineScope(role ? role.scope_tags || [] : [])
    setStartFrom(template ? template.name : null)
    setRevision(role?.revision)
    setReason('')
    setServerErrors({})
    setConflict(null)
    setSubmitted(false)
    setTriedReview(false)
  }, [open, role, template])

  const generated = useMemo(() => slugify(displayName), [displayName])
  const identifier = nameTouched ? name : generated
  // e.g. an Arabic display name: the server generates role_xxxxxx.
  const autoIdentifier = !nameTouched && displayName.trim().length >= 2 && generated.length < 2

  const errors = {
    display_name:
      displayName.trim().length < 2 ? 'Enter a display name of at least 2 characters'
      : displayName.trim().length > 100 ? 'Display name can be at most 100 characters'
      : serverErrors.display_name,
    name:
      autoIdentifier || (!nameTouched && !displayName.trim()) ? undefined
      : !NAME_RE.test(identifier) ? 'Use 2–50 lowercase letters, digits, “_” or “-”, starting with a letter or digit'
      : RESERVED.has(identifier) && !(isEdit && identifier === role?.name) ? `“${identifier}” is reserved; choose another identifier`
      : serverErrors.name,
    description: description.length > 2000 ? 'Description can be at most 2000 characters' : undefined,
    scope: scopeMode === 'tags' && cleanTags(scopeTags).length === 0 ? 'Choose at least one tag, or allow all resources' : undefined,
  }
  const detailsValid = !errors.display_name && !errors.name && !errors.description
  const valid = detailsValid && !errors.scope

  const full = granted.has(superPerm)
  const effectiveScope = scopeMode === 'tags' ? cleanTags(scopeTags) : []
  const scopeChanged = !sameTags(effectiveScope, baselineScope)
  const added = useMemo(() => [...granted].filter((p) => !baseline.has(p)).sort(), [granted, baseline])
  const removed = useMemo(() => [...baseline].filter((p) => !granted.has(p)).sort(), [granted, baseline])
  const elevatedAdded = added.filter((p) => isElevated(riskOf(catalog, p)))
  const canGrant = (id: string) => hasPermission(me, id)
  const beyondYou = [...granted].filter((p) => !canGrant(p) && !baseline.has(p))
  const permissionsChanged = added.length > 0 || removed.length > 0
  const dirty = isEdit
    ? permissionsChanged || scopeChanged || displayName !== role!.display_name || description !== (role!.description || '') || identifier !== role!.name
    : displayName.trim() !== '' || description !== '' || granted.size > 0 || effectiveScope.length > 0

  const templates = useMemo(() => {
    const byName = new Map((roles || []).map((r) => [r.name, r]))
    return TEMPLATE_ORDER.map((n) => byName.get(n)).filter((r): r is Role => !!r && !r.permissions.includes(superPerm))
  }, [roles, superPerm])

  function applyTemplate(t: Role | null) {
    setStartFrom(t ? t.name : '')
    setGranted(new Set(t?.permissions || []))
    if (!description.trim() || templates.some((x) => x.description === description)) setDescription(t?.description || '')
  }

  const impact = useQuery<Impact>({
    queryKey: ['roles', 'impact', role?.id, [...granted].sort().join(',')],
    queryFn: async () => (await api.post(`/roles/${role!.id}/impact`, { permissions: [...granted] })).data,
    enabled: open && isEdit && step === 'review' && permissionsChanged,
    staleTime: 0,
  })

  const save = useMutation({
    mutationFn: async (overrideRevision?: number) => {
      const payload: Record<string, unknown> = {
        display_name: displayName.trim(),
        description: description.trim(),
        permissions: [...granted],
        scope_tags: effectiveScope,
        reason: reason.trim() || undefined,
      }
      if (nameTouched || isEdit) { if (!autoIdentifier) payload.name = identifier }
      if (isEdit) {
        payload.expected_revision = overrideRevision ?? revision
        return (await api.put(`/roles/${role!.id}`, payload)).data
      }
      return (await api.post('/roles', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Role updated' : 'Role created', isEdit ? 'Members get the change within about 5 seconds and stay signed in.' : 'Assign it to users in the Users tab.')
      qc.invalidateQueries({ queryKey: ['roles'] })
      qc.invalidateQueries({ queryKey: ['users'] })
      // The signed-in user's own role may just have changed.
      void hydrate()
      onOpenChange(false)
    },
    onError: (e: any) => {
      const f = describeFailure(e)
      if (f.kind === 'conflict' && f.code === 'role_revision_conflict') {
        setConflict(f.detail?.current as Role)
        return
      }
      const msg = f.message
      if (f.status === 409 && /identifier/i.test(msg)) { setServerErrors({ name: msg }); setStep('details') }
      else if (f.status === 400 && /identifier|reserved/i.test(msg)) { setServerErrors({ name: msg }); setStep('details') }
      else if (f.status === 422 && /display name/i.test(msg)) { setServerErrors({ display_name: msg }); setStep('details') }
      else setServerErrors({ general: msg })
    },
  })

  const guard = useDirtyGuard(dirty && !save.isSuccess, save.isPending, onOpenChange)

  const stepIndex = STEPS.findIndex((s) => s.id === step)
  function go(next: Step) {
    setSubmitted(true)
    // Leaving Details needs a valid name; every other step can be revisited freely.
    if (step === 'details' && next !== 'details' && !detailsValid) return
    if (next === 'review') setTriedReview(true)
    if (next === 'review' && !valid) { if (!detailsValid) setStep('details'); else setStep('visibility'); return }
    setServerErrors((e) => ({ ...e, general: undefined }))
    setStep(next)
  }
  function submit() {
    setSubmitted(true)
    setTriedReview(true)
    setServerErrors({})
    if (!valid || !catalog) { setStep(!detailsValid ? 'details' : 'visibility'); return }
    save.mutate(undefined)
  }

  const title = isEdit ? `Edit role · ${role?.display_name}` : template ? `New role from ${template.display_name}` : 'New role'

  return (
    <>
      <Dialog open={open} onOpenChange={guard.onOpenChange}>
        <DialogContent className="flex h-[min(860px,calc(100dvh-1rem))] max-w-6xl flex-col gap-0 overflow-hidden p-0 sm:p-0">
          <DialogHeader className="border-b border-border px-5 py-4 pr-12">
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              {isEdit && role?.user_count
                ? `${role.user_count} ${role.user_count === 1 ? 'user has' : 'users have'} this role. Saved changes reach them within about 5 seconds; nobody is signed out.`
                : 'A role decides what its members can open and change, and optionally which tagged resources they see.'}
            </DialogDescription>
          </DialogHeader>

          <div className="flex min-h-0 flex-1 flex-col md:flex-row">
            {/* Step rail */}
            <nav aria-label="Role editor steps" className="shrink-0 border-b border-border bg-surface2/30 md:w-60 md:border-b-0 md:border-r">
              <ol className="flex gap-1 overflow-x-auto p-2 md:flex-col md:p-3">
                {STEPS.map((s, i) => {
                  const active = s.id === step
                  const done = i < stepIndex
                  const bad = (s.id === 'details' && submitted && !detailsValid) || (s.id === 'visibility' && triedReview && !!errors.scope)
                  return (
                    <li key={s.id} className="shrink-0">
                      <button
                        type="button"
                        onClick={() => go(s.id)}
                        aria-current={active ? 'step' : undefined}
                        className={cn(
                          'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors',
                          active ? 'bg-primary/10 text-primary' : 'text-text2 hover:bg-surface2',
                        )}
                      >
                        <span className={cn(
                          'flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold',
                          bad ? 'border-danger text-danger' : active ? 'border-primary bg-primary text-white' : done ? 'border-success/50 bg-success/10 text-success' : 'border-border text-muted',
                        )}>
                          {bad ? '!' : done ? <Check className="h-3.5 w-3.5" aria-hidden /> : i + 1}
                        </span>
                        <span className="min-w-0">
                          <span className="block text-xs font-semibold">{s.label}</span>
                          <span className="hidden truncate text-[10.5px] text-muted md:block">{s.hint}</span>
                        </span>
                      </button>
                    </li>
                  )
                })}
              </ol>
              {/* Running summary */}
              <div className="hidden space-y-3 border-t border-border px-4 py-3 md:block">
                <SummaryLine label="Permissions" value={full ? 'Full administration' : `${granted.size} selected`} warn={full || elevatedAdded.length > 0} />
                <div>
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Visibility</div>
                  {full ? <span className="text-xs text-muted">Everything (bypasses scope)</span> : <ScopeChips tags={effectiveScope} empty="All resources" max={4} />}
                </div>
                {step !== 'access' && (
                  <div>
                    <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Menu</div>
                    <MenuPreview permissions={granted} compact />
                  </div>
                )}
              </div>
            </nav>

            {/* Step content */}
            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
              {conflict && (
                <div role="alert" className="mb-4 space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
                  <div className="flex items-center gap-2 font-medium"><AlertTriangle className="h-4 w-4 text-warning" /> Someone else changed this role</div>
                  <p className="text-xs text-muted">Revision {conflict.revision} was saved{conflict.updated_by ? ` by ${conflict.updated_by}` : ''} after you opened it. Reload to review their version, or keep your edits and overwrite it.</p>
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" variant="outline" onClick={() => {
                      setDisplayName(conflict.display_name); setDescription(conflict.description || '')
                      setGranted(new Set(conflict.permissions)); setBaseline(new Set(conflict.permissions))
                      const t = conflict.scope_tags || []
                      setScopeTags(t); setScopeMode(t.length ? 'tags' : 'all'); setBaselineScope(t)
                      setName(conflict.name); setRevision(conflict.revision); setConflict(null)
                    }}><RefreshCw className="h-3.5 w-3.5" /> Reload latest (discard my edits)</Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => {
                      setRevision(conflict.revision); setBaseline(new Set(conflict.permissions)); setBaselineScope(conflict.scope_tags || []); setConflict(null)
                    }}>Keep my edits and overwrite</Button>
                  </div>
                </div>
              )}
              {serverErrors.general && <div role="alert" className="mb-4 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">{serverErrors.general}</div>}

              {step === 'details' && (
                <div className="space-y-5">
                  {!isEdit && templates.length > 0 && (
                    <fieldset>
                      <legend className="mb-1 text-sm font-semibold">Start from</legend>
                      <p className="mb-2 text-xs text-muted">Pick the closest team role and adjust it, or start blank. You can change everything in the next steps.</p>
                      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                        <TemplateCard
                          icon={Plus}
                          title="Blank role"
                          detail="No permissions; choose them yourself."
                          selected={startFrom === '' || (startFrom === null && granted.size === 0)}
                          onClick={() => applyTemplate(null)}
                        />
                        {templates.map((t) => (
                          <TemplateCard
                            key={t.name}
                            icon={TEMPLATE_ICONS[t.name] || PanelsTopLeft}
                            title={t.display_name}
                            detail={t.description}
                            count={t.permissions.length}
                            team={t.name.endsWith('_team')}
                            selected={startFrom === t.name}
                            onClick={() => applyTemplate(t)}
                          />
                        ))}
                      </div>
                    </fieldset>
                  )}
                  {template?.permissions.includes(superPerm) && !isEdit && (
                    <div role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                      <span>You are copying <strong>Full administration</strong>. The copy is just as powerful as the original. Turn it off in Access to build a narrower role.</span>
                    </div>
                  )}

                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <FormField label="Display name" required error={(submitted || displayName) ? errors.display_name : undefined}>
                      <Input value={displayName} maxLength={120} onChange={(e) => { setDisplayName(e.target.value); setServerErrors({}) }} placeholder="e.g. Network Team – Riyadh" autoFocus={!isEdit} aria-invalid={!!errors.display_name} />
                    </FormField>
                    <FormField
                      label="Identifier"
                      error={(submitted || nameTouched) ? errors.name : undefined}
                      hint={autoIdentifier
                        ? 'No Latin letters in the display name, so an identifier like role_a1b2c3 is generated. Type your own to override.'
                        : isEdit ? 'Renaming carries assigned users and directory mappings along automatically.' : 'Stable name used by directory (LDAP/SSO) mappings and the audit log.'}
                    >
                      <Input value={autoIdentifier ? '' : identifier} onChange={(e) => { setName(e.target.value.toLowerCase()); setNameTouched(true); setServerErrors({}) }} placeholder={autoIdentifier ? 'role_… (generated)' : 'network_riyadh'} className="font-mono text-xs" aria-invalid={!!errors.name} />
                    </FormField>
                  </div>
                  <FormField label="Description" error={errors.description} hint={`${description.length}/2000 · shown next to the role when assigning it`}>
                    <Textarea value={description} rows={3} onChange={(e) => setDescription(e.target.value)} placeholder="Who this role is for and what it should allow…" />
                  </FormField>
                </div>
              )}

              {step === 'access' && (
                <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_15rem]">
                  <div className="min-w-0 space-y-3">
                    <div>
                      <h3 className="text-sm font-semibold">What can members open and change?</h3>
                      <p className="text-xs text-muted">Choose a level per module. The menu follows these permissions: members only see the areas their role grants. Sensitive and privileged permissions are never part of a level; open a module’s permissions to tick them one by one.</p>
                    </div>
                    {full && (
                      <p role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                        Full administration grants every permission, including user, security and system management, and ignores visibility scope. Untick it under Administration → System Administration to choose access module by module.
                      </p>
                    )}
                    {beyondYou.length > 0 && (
                      <p role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                        You cannot grant permissions you do not hold yourself: {beyondYou.join(', ')}. Remove them or ask an administrator to save this role.
                      </p>
                    )}
                    <AccessLevelPicker
                      modules={modules}
                      granted={granted}
                      onChange={setGranted}
                      onClear={!full && granted.size > 0 ? () => setGranted(new Set()) : undefined}
                      superPerm={superPerm}
                      canGrant={canGrant}
                      baseline={isEdit ? baseline : undefined}
                    />
                  </div>
                  <aside className="h-fit rounded-lg border border-border bg-surface2/30 p-3 lg:sticky lg:top-0">
                    <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold"><PanelsTopLeft className="h-3.5 w-3.5 text-primary" aria-hidden /> Menu members will see</div>
                    <MenuPreview permissions={granted} />
                  </aside>
                </div>
              )}

              {step === 'visibility' && (
                <VisibilityStep
                  full={full}
                  mode={scopeMode}
                  onMode={setScopeMode}
                  tags={scopeTags}
                  onTags={setScopeTags}
                  error={triedReview ? errors.scope : undefined}
                />
              )}

              {step === 'review' && (
                <div className="space-y-4">
                  <div className="grid gap-3 md:grid-cols-2">
                    <ReviewCard title="Role">
                      <div className="text-sm font-semibold">{displayName.trim() || '—'}</div>
                      <div className="font-mono text-[11px] text-muted">{autoIdentifier ? 'role_… (generated)' : identifier}</div>
                      {description.trim() && <p className="mt-1 text-xs text-text2">{description.trim()}</p>}
                    </ReviewCard>
                    <ReviewCard title="Visibility">
                      {full
                        ? <p className="text-xs text-muted">Full administration sees everything; scope does not apply.</p>
                        : effectiveScope.length
                          ? <><ScopeChips tags={effectiveScope} max={12} /><p className="mt-1 text-[11px] text-muted">Members see only resources (and their alerts) tagged with any of these. Users’ own scope tags add to them.</p></>
                          : <p className="text-xs text-muted">All resources the permissions allow.</p>}
                      {isEdit && scopeChanged && <p className="mt-1 text-[11px] font-medium text-primary">Changed from: {baselineScope.length ? baselineScope.join(', ') : 'all resources'}</p>}
                    </ReviewCard>
                    <ReviewCard title="Access by area">
                      <AccessSummary catalog={catalog} granted={granted} superPerm={superPerm} />
                    </ReviewCard>
                    <ReviewCard title="Menu members will see">
                      <MenuPreview permissions={granted} />
                    </ReviewCard>
                  </div>

                  {isEdit && (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <ChangeList title="Granting" tone="success" ids={added} catalog={catalog} empty="Nothing new" />
                      <ChangeList title="Removing" tone="danger" ids={removed} catalog={catalog} empty="Nothing removed" />
                    </div>
                  )}
                  {elevatedAdded.length > 0 && (
                    <p role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                      This grants privileged access: {elevatedAdded.join(', ')}. Anyone with this role can change access or shared infrastructure.
                    </p>
                  )}
                  {beyondYou.length > 0 && (
                    <p role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">You cannot grant: {beyondYou.join(', ')}. The server will refuse this save.</p>
                  )}
                  {isEdit && (permissionsChanged || scopeChanged) && (
                    <div className="rounded-md border border-border p-3 text-sm">
                      <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Who is affected</div>
                      {!permissionsChanged && <p className="text-xs">{role!.user_count} {role!.user_count === 1 ? 'account has' : 'accounts have'} this role; what they can see changes within about 5 seconds.</p>}
                      {permissionsChanged && impact.isLoading && <div className="flex items-center gap-2 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Calculating impact…</div>}
                      {permissionsChanged && impact.isError && <p className="text-xs text-danger">Could not calculate the impact: {describeFailure(impact.error).message}. You can still save; the server enforces the rules.</p>}
                      {permissionsChanged && impact.data && (
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
                    <Textarea value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="e.g. Riyadh network team needs config backups" />
                  </FormField>
                </div>
              )}
            </div>
          </div>

          {/* Footer */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-surface px-5 py-3">
            <span className="text-[11px] text-muted">Step {stepIndex + 1} of {STEPS.length} · {STEPS[stepIndex].label}</span>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" onClick={() => guard.onOpenChange(false)} disabled={save.isPending}>Cancel</Button>
              {stepIndex > 0 && (
                <Button type="button" variant="outline" onClick={() => setStep(STEPS[stepIndex - 1].id)} disabled={save.isPending}>
                  <ChevronLeft className="h-4 w-4" /> Back
                </Button>
              )}
              {step !== 'review' ? (
                <Button type="button" onClick={() => go(STEPS[stepIndex + 1].id)} disabled={!catalog}>
                  {step === 'visibility' ? 'Review' : `Next: ${STEPS[stepIndex + 1].label}`} <ChevronRight className="h-4 w-4" />
                </Button>
              ) : (
                <Button
                  type="button"
                  onClick={submit}
                  disabled={save.isPending || !catalog || beyondYou.length > 0 || (isEdit && !dirty)}
                  variant={elevatedAdded.length ? 'destructive' : 'default'}
                >
                  {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                  {isEdit ? (dirty ? 'Save changes' : 'No changes to save') : 'Create role'}
                </Button>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>
      {guard.confirm}
    </>
  )
}

function SummaryLine({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('text-xs font-medium', warn ? 'text-warning' : 'text-text')}>{value}</div>
    </div>
  )
}

function TemplateCard({ icon: Icon, title, detail, count, team, selected, onClick }: {
  icon: typeof Network; title: string; detail?: string; count?: number; team?: boolean; selected: boolean; onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        'flex h-full flex-col items-start gap-1.5 rounded-lg border p-3 text-left transition-colors',
        selected ? 'border-primary bg-primary/[0.06] ring-1 ring-primary/40' : 'border-border hover:border-primary/40 hover:bg-surface2/50',
      )}
    >
      <span className="flex w-full items-center gap-2">
        <span className={cn('flex h-7 w-7 items-center justify-center rounded-md', selected ? 'bg-primary text-white' : 'bg-surface2 text-primary')}>
          <Icon className="h-3.5 w-3.5" aria-hidden />
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-semibold">{title}</span>
        {selected && <Check className="h-3.5 w-3.5 text-primary" aria-hidden />}
      </span>
      {detail && <span className="line-clamp-2 text-[11px] leading-snug text-muted">{detail}</span>}
      <span className="mt-auto flex gap-1.5 text-[10px] text-muted">
        {team && <span className="rounded bg-info/10 px-1 font-semibold uppercase tracking-wide text-info">Team</span>}
        {count !== undefined && <span>{count} permissions</span>}
      </span>
    </button>
  )
}

function VisibilityStep({ full, mode, onMode, tags, onTags, error }: {
  full: boolean; mode: 'all' | 'tags'; onMode: (m: 'all' | 'tags') => void; tags: string[]; onTags: (t: string[]) => void; error?: string
}) {
  const { data: defs } = useTags()
  const byName = useMemo(() => new Map((defs || []).map((d) => [d.name.toLowerCase(), d])), [defs])
  const options: { id: 'all' | 'tags'; title: string; detail: string; icon: typeof Globe2 }[] = [
    { id: 'all', title: 'All resources', detail: 'Members see every device, server, service check and application their permissions allow.', icon: Globe2 },
    { id: 'tags', title: 'Only resources with these tags', detail: 'Limit members to their team’s resources, e.g. tag “network” or “riyadh”. Everything else is hidden from them.', icon: Tags },
  ]
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold">Which resources can members see?</h3>
        <p className="text-xs text-muted">Permissions decide which areas a member can open; visibility decides which devices, servers, service checks and applications (and their alerts) they see inside those areas.</p>
      </div>
      {full && (
        <p role="note" className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" /> Full administration always sees everything, so visibility tags have no effect while it is granted.
        </p>
      )}
      <div role="radiogroup" aria-label="Visibility" className="grid gap-2 md:grid-cols-2">
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={mode === o.id}
            onClick={() => onMode(o.id)}
            className={cn(
              'flex items-start gap-3 rounded-lg border p-3 text-left transition-colors',
              mode === o.id ? 'border-primary bg-primary/[0.06] ring-1 ring-primary/40' : 'border-border hover:border-primary/40',
            )}
          >
            <span className={cn('mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border', mode === o.id ? 'border-primary' : 'border-border-strong')}>
              {mode === o.id && <span className="h-2 w-2 rounded-full bg-primary" />}
            </span>
            <span className="min-w-0">
              <span className="flex items-center gap-1.5 text-sm font-medium"><o.icon className="h-3.5 w-3.5 text-primary" aria-hidden />{o.title}</span>
              <span className="mt-0.5 block text-xs text-muted">{o.detail}</span>
            </span>
          </button>
        ))}
      </div>

      {mode === 'tags' && (
        <div className="space-y-2 rounded-lg border border-border p-3">
          <label className="text-xs font-medium">Visibility tags</label>
          <TagPicker value={tags} onChange={onTags} allowCreate={false} placeholder="Add tags, e.g. network, riyadh…" />
          <p className={cn('text-[11px]', error ? 'text-danger' : 'text-muted')}>
            {error || 'Members see resources carrying ANY of these tags. Untagged resources are hidden from them. A user’s own scope tags (set on the user) add to these.'}
          </p>
          {tags.length > 0 && (
            <div className="overflow-hidden rounded-md border border-border">
              <table className="w-full text-xs">
                <caption className="sr-only">Resources each tag currently covers</caption>
                <thead className="bg-surface2/50 text-[10px] uppercase tracking-wider text-muted">
                  <tr><th className="px-2.5 py-1.5 text-left font-medium">Tag</th><th className="px-2.5 py-1.5 text-right font-medium">Devices</th><th className="px-2.5 py-1.5 text-right font-medium">Servers</th><th className="px-2.5 py-1.5 text-right font-medium">Service checks</th><th className="px-2.5 py-1.5 text-right font-medium">Applications</th></tr>
                </thead>
                <tbody>
                  {tags.map((t) => {
                    const d = byName.get(t.toLowerCase())
                    const total = d ? d.device_count + d.server_count + d.service_count + d.app_count : 0
                    return (
                      <tr key={t} className="border-t border-border">
                        <td className="px-2.5 py-1.5"><ScopeChips tags={[t]} />{d && total === 0 && <span className="ml-1.5 text-[10px] text-warning">nothing tagged yet</span>}</td>
                        <td className="px-2.5 py-1.5 text-right tabular-nums">{d?.device_count ?? '—'}</td>
                        <td className="px-2.5 py-1.5 text-right tabular-nums">{d?.server_count ?? '—'}</td>
                        <td className="px-2.5 py-1.5 text-right tabular-nums">{d?.service_count ?? '—'}</td>
                        <td className="px-2.5 py-1.5 text-right tabular-nums">{d?.app_count ?? '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ReviewCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border p-3">
      <h4 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</h4>
      {children}
    </section>
  )
}

/** "Network: Devices (Full), NetFlow (View) …" — one line per area with access. */
export function AccessSummary({ catalog, granted, superPerm }: { catalog?: Catalog; granted: Set<string>; superPerm: string }) {
  if (granted.has(superPerm)) return <p className="flex items-center gap-1.5 text-xs font-medium text-warning"><ShieldAlert className="h-3.5 w-3.5" aria-hidden /> Full administration: every permission</p>
  const areas = groupByArea(catalog?.modules || [])
    .map(({ area, modules }) => ({
      area,
      items: modules
        .map((m) => {
          const level = moduleLevel(m, granted)
          if (level === 'none') return null
          const label = level === 'full' ? fullLabel(m) : level === 'view' ? 'View' : levelsFor(m).length === 0 ? m.permissions.filter((p) => granted.has(p.id)).map((p) => p.label).join(', ') : 'Custom'
          return `${m.label} (${label})`
        })
        .filter(Boolean) as string[],
    }))
    .filter((a) => a.items.length > 0)
  if (areas.length === 0) return <p className="text-xs text-muted">No permissions selected.</p>
  return (
    <dl className="space-y-1 text-xs">
      {areas.map((a) => (
        <div key={a.area} className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2">
          <dt className="font-medium text-text">{a.area}</dt>
          <dd className="text-text2">{a.items.join(' · ')}</dd>
        </div>
      ))}
    </dl>
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
