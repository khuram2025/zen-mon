import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  History,
  ListChecks,
  Loader2,
  Play,
  Plus,
  Save,
  Trash2,
  X,
} from 'lucide-react'
import { regionalDate } from '@/lib/regionalTime'
import { useCan } from '@/stores/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { discoveryApi, ruleBody, rulesApi } from './api'
import type {
  AssetKind,
  CatalogAction,
  Enforcement,
  CatalogField,
  ConditionGroup,
  ConditionLeaf,
  DiscoveryRule,
  DiscoveryRun,
  GroupOp,
  ImportMode,
  OnUnmatch,
  PreviewResponse,
  RuleAction,
  RuleCatalog,
  RuleDraft,
  RuleMode,
  RuleEngineSettings,
  RuleTrigger,
  RuleVersion,
} from './types'
import {
  DEFAULT_LIMITS,
  DECISION_LABEL,
  ENFORCEMENT_HELP,
  ENFORCEMENT_LABEL,
  MODE_HELP,
  MODE_LABEL,
  MODE_ORDER,
  TRIGGER_LABEL,
  ASSET_KIND_LABEL,
  ASSET_KIND_ORDER,
  ON_UNMATCH_HELP,
  ON_UNMATCH_LABEL,
  activationPermissions,
  dependencyIds,
  fieldSourceHint,
  kindsPhrase,
  pickableActions,
  IMPORT_AS_LABEL,
  IMPORT_MODE_TEXT,
  Lookups,
  Path,
  actionProblems,
  addGroup,
  addLeaf,
  canAddGroup,
  canAddLeaf,
  changeLeafField,
  changeLeafOperator,
  conditionProblems,
  countLeaves,
  defaultActionValue,
  defaultLeaf,
  defaultRule,
  fieldOf,
  isGroup,
  listText,
  operatorOf,
  parseList,
  removeNode,
  ruleProblems,
  setGroupOp,
  summarizeAction,
  summarizeActions,
  sentenceCase,
  summarizeCondition,
  summarizePreview,
  updateNode,
} from './ruleModel'
import {
  DevicePicker,
  DispositionBadge,
  ModeBadge,
  PlanDetails,
  PlanWhy,
  RefOption,
  useRuleCatalog,
  useRuleLookups,
  useWithDeviceNames,
} from './ruleUi'

const TRIGGERS: RuleTrigger[] = ['discovery', 'created', 'changed']
const KIND_EDITOR_LABEL: Record<AssetKind, string> = {
  device: 'Network devices',
  server: 'Servers (Windows / Linux / ESXi hosts)',
  child: ASSET_KIND_LABEL.child,
}


function draftOf(rule: DiscoveryRule): RuleDraft {
  return {
    name: rule.name,
    description: rule.description ?? null,
    mode: rule.mode,
    asset_kinds: rule.asset_kinds?.length ? rule.asset_kinds : ['device'],
    profile_ids: rule.profile_ids?.length ? rule.profile_ids.map(String) : null,
    condition: isGroup(rule.condition) ? rule.condition : { op: 'all', conditions: [] },
    actions: rule.actions || [],
    stop_processing: !!rule.stop_processing,
    triggers: rule.triggers?.length ? rule.triggers.filter((t): t is RuleTrigger => TRIGGERS.includes(t)) : ['discovery'],
    enforcement: rule.enforcement === 'enforce' ? 'enforce' : 'set_once',
    on_unmatch: rule.on_unmatch === 'revert' ? 'revert' : 'keep',
  }
}

export function RuleEditorPage() {
  const { id } = useParams<{ id: string }>()
  const isNew = !id
  const navigate = useNavigate()
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('discovery.manage_rules')
  const { data: catalog, error: catalogError } = useRuleCatalog()
  const { lookups: baseLookups, options } = useRuleLookups()
  // Settings carry the approval limit the "whenever they change" trigger warns about.
  const { data: settings } = useQuery<RuleEngineSettings>({ queryKey: ['discovery', 'rules', 'settings'], queryFn: rulesApi.getSettings })

  const [draft, setDraft] = useState<RuleDraft>(defaultRule)
  const [revision, setRevision] = useState<number | null>(null)
  const [loadedId, setLoadedId] = useState<string | null>(null)
  const [position, setPosition] = useState<'first' | 'last'>('last')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [showProblems, setShowProblems] = useState(false)

  const { data: rule, error: ruleError, isLoading: ruleLoading } = useQuery<DiscoveryRule>({
    queryKey: ['discovery', 'rules', 'rule', id],
    queryFn: () => rulesApi.get(id!),
    enabled: !isNew,
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    if (rule && rule.id !== loadedId) {
      setDraft(draftOf(rule))
      setRevision(rule.revision)
      setLoadedId(rule.id)
    }
  }, [rule, loadedId])

  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(draft.actions), [draft.actions]))

  const update = (patch: Partial<RuleDraft>) => {
    setDraft((d) => ({ ...d, ...patch }))
    setSaveError(null)
  }

  const problems = useMemo(() => ruleProblems(draft, catalog), [draft, catalog])

  const save = useMutation({
    mutationFn: () => (isNew ? rulesApi.create(draft, position) : rulesApi.update(id!, draft, revision)),
    onSuccess: (saved) => {
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'list'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'versions', saved.id] })
      qc.setQueryData(['discovery', 'rules', 'rule', saved.id], saved)
      setDraft(draftOf(saved))
      setRevision(saved.revision)
      setLoadedId(saved.id)
      setSaveError(null)
      toast.success(isNew ? 'Rule created' : 'Rule saved', `${saved.name} · ${saved.mode}`)
      if (isNew) navigate(`/discovery/rules/${saved.id}`, { replace: true })
    },
    onError: (e: any) => {
      const status = e?.response?.status
      let msg = apiErrorMessage(e)
      if (status === 409 && !isNew && /revision|newer|changed/i.test(msg)) {
        msg += ' Reload the page to see the newer version; your changes are still here until you leave.'
      }
      setSaveError(msg)
      toast.error(status === 403 ? 'Not allowed' : 'Could not save the rule', msg)
    },
  })

  const onSave = () => {
    setShowProblems(true)
    if (problems.length) return
    save.mutate()
  }

  if (!isNew && ruleLoading) {
    return (
      <Card>
        <CardContent className="py-16 text-center">
          <Loader2 className="mx-auto h-6 w-6 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted">Loading rule…</p>
        </CardContent>
      </Card>
    )
  }
  if (!isNew && ruleError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm">
          <AlertTriangle className="mx-auto h-8 w-8 text-warning" />
          <p className="mt-2">{apiErrorMessage(ruleError, 'Rule not found')}</p>
          <Button variant="outline" className="mt-4" onClick={() => navigate('/discovery/rules')}>
            Back to rules
          </Button>
        </CardContent>
      </Card>
    )
  }

  const limits = catalog?.limits ?? DEFAULT_LIMITS

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <button
            onClick={() => navigate('/discovery/rules')}
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to rules
          </button>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <ListChecks className="h-6 w-6 text-primary" />
            {isNew ? 'New discovery rule' : rule?.name || 'Discovery rule'}
            {!isNew && rule && <ModeBadge mode={rule.mode} />}
          </h1>
          <p className="mt-1 text-xs text-muted">
            {isNew
              ? 'When a scan finds something that matches, decide what happens to it and what it gets on import.'
              : `Revision ${revision ?? '—'}${rule?.updated_at ? ` · saved ${relativeTime(rule.updated_at)}` : ''}${rule ? ` · priority ${rule.priority}` : ''}`}
          </p>
        </div>
        {canManage && (
          <Button onClick={onSave} disabled={save.isPending || !catalog}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {isNew ? 'Create rule' : 'Save'}
          </Button>
        )}
      </div>

      {!canManage && (
        <div className="rounded-md border border-border bg-surface2/50 px-3 py-2 text-xs text-muted">
          You can view this rule and preview it. Changing rules needs the Discovery “Manage rules” permission.
        </div>
      )}
      {catalogError && (
        <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger-fg">
          Could not load the rule catalog: {apiErrorMessage(catalogError)}
        </div>
      )}

      <fieldset disabled={!canManage} className="min-w-0 space-y-5">
        {/* Name */}
        <Card>
          <CardContent className="grid gap-3 p-4 md:grid-cols-2">
            <FormField label="Name" required>
              <Input
                value={draft.name}
                maxLength={150}
                placeholder="Fortinet firewalls in the DC range"
                onChange={(e) => update({ name: e.target.value })}
              />
            </FormField>
            <FormField label="Description">
              <Textarea
                rows={1}
                maxLength={1000}
                value={draft.description ?? ''}
                placeholder="Why this rule exists (optional)"
                onChange={(e) => update({ description: e.target.value })}
              />
            </FormField>
          </CardContent>
        </Card>

        {/* When */}
        <Section title="When" hint="Which scan results, devices and servers this rule looks at.">
          <div className="space-y-3">
            <div className="flex flex-wrap gap-4">
              {((catalog?.asset_kinds?.map((k) => k.key) ?? ASSET_KIND_ORDER) as AssetKind[]).map((k) => (
                <label key={k} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={draft.asset_kinds.includes(k)}
                    onChange={(e) =>
                      update({
                        asset_kinds: e.target.checked
                          ? Array.from(new Set([...draft.asset_kinds, k]))
                          : draft.asset_kinds.filter((x) => x !== k),
                      })
                    }
                  />
                  {KIND_EDITOR_LABEL[k] ?? catalog?.asset_kinds?.find((x) => x.key === k)?.label ?? k}
                </label>
              ))}
            </div>
            {draft.asset_kinds.includes('child') && (
              <div className="text-xs text-muted">
                FortiLink switches and FortiAPs are read through their FortiGate and never polled themselves. Only
                group, location, owner, tags and network map apply to them; the rule matches them on the facts the
                controller reports.
              </div>
            )}
            <ProfileLimit
              value={draft.profile_ids}
              options={options.discovery_profiles}
              onChange={(profile_ids) => update({ profile_ids })}
            />
          </div>
        </Section>

        {/* Runs automatically on */}
        <Section
          title="Runs automatically on"
          hint="Rules → Run on inventory can also run any active rule on the devices and servers you choose."
        >
          <div className="space-y-2">
            <div className="flex flex-wrap gap-4">
              {((catalog?.triggers?.map((t) => t.key) ?? TRIGGERS) as RuleTrigger[]).map((t) => (
                <label key={t} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={draft.triggers.includes(t)}
                    onChange={(e) =>
                      update({
                        triggers: e.target.checked
                          ? Array.from(new Set([...draft.triggers, t]))
                          : draft.triggers.filter((x) => x !== t),
                      })
                    }
                  />
                  {catalog?.triggers?.find((x) => x.key === t)?.label ?? TRIGGER_LABEL[t] ?? t}
                </label>
              ))}
            </div>
            {draft.triggers.length === 0 && <div className="text-xs text-warning-fg">Choose at least one.</div>}
            {draft.triggers.includes('created') && (
              <div className="text-xs text-muted">
                New devices and servers are checked shortly after they are added (Add Device, CSV import, agent
                enrolment); the rule sets values and adds tags, it never removes anything or turns monitoring off.
              </div>
            )}
            {draft.triggers.includes('changed') && (
              <div className="text-xs text-muted">
                Re-checked once a minute on the devices and servers whose facts changed, and on every one of them after
                this rule changes. What “When an asset stops matching” says below decides what happens to the values
                on assets that no longer match.
              </div>
            )}
            {draft.triggers.includes('changed') && draft.mode === 'active' && (
              <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-fg">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  Active rules with this trigger apply on their own within a minute; more than{' '}
                  {settings?.max_changes_per_run ?? 100} changes wait for approval.
                </span>
              </div>
            )}
          </div>
        </Section>

        {/* If */}
        <Section
          title="If"
          hint={
            catalog
              ? `${sentenceCase(summarizeCondition(draft.condition, catalog, lookups))}. (${countLeaves(draft.condition)} of ${limits.max_leaves} conditions)`
              : undefined
          }
        >
          {catalog ? (
            <GroupEditor
              root={draft.condition}
              node={draft.condition}
              path={[]}
              catalog={catalog}
              options={options}
              kinds={draft.asset_kinds}
              onChange={(condition) => update({ condition })}
            />
          ) : (
            <div className="text-sm text-muted">Loading fields…</div>
          )}
        </Section>

        {/* Then */}
        <Section title="Then" hint="The first matching rule sets each setting; tags from every matching rule are merged.">
          {catalog ? (
            <ActionsEditor
              draft={draft}
              catalog={catalog}
              options={options}
              lookups={lookups}
              can={can}
              onChange={(actions) => update({ actions })}
            />
          ) : (
            <div className="text-sm text-muted">Loading actions…</div>
          )}
        </Section>

        {/* Options */}
        <Section title="Options">
          <div className="grid gap-4 md:grid-cols-3">
            <label className="flex items-start gap-2 text-sm md:col-span-3">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={draft.stop_processing}
                onChange={(e) => update({ stop_processing: e.target.checked })}
              />
              <span>
                Stop processing further rules when this rule matches
                <span className="block text-xs text-muted">Rules below it are not evaluated for that result.</span>
              </span>
            </label>
            <FormField label="Mode" hint={MODE_HELP[draft.mode] ?? 'Preview works in every mode.'}>
              <Select value={draft.mode} onValueChange={(v) => update({ mode: v as RuleMode })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(catalog?.modes?.map((m) => m.key) ?? MODE_ORDER).map((m) => (
                    <SelectItem key={m} value={m}>
                      {catalog?.modes?.find((x) => x.key === m)?.label ?? MODE_LABEL[m] ?? m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            {isNew && (
              <FormField label="Place in the rule order">
                <Select value={position} onValueChange={(v) => setPosition(v as 'first' | 'last')}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="last">Last (lowest priority)</SelectItem>
                    <SelectItem value="first">First (highest priority)</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            )}
            {draft.mode === 'active' && catalog && (
              <ActivationNote actions={draft.actions} assetKinds={draft.asset_kinds} catalog={catalog} can={can} />
            )}
            <div className="space-y-1.5 md:col-span-3">
              <div className="text-sm font-medium">When a value was changed by hand</div>
              <div className="flex flex-wrap gap-4 text-sm">
                {(['set_once', 'enforce'] as Enforcement[]).map((k) => (
                  <label key={k} className="flex items-center gap-2">
                    <input type="radio" name="enforcement" checked={draft.enforcement === k} onChange={() => update({ enforcement: k })} />
                    {ENFORCEMENT_LABEL[k]}
                  </label>
                ))}
              </div>
              <div className="text-xs text-muted">
                {ENFORCEMENT_HELP[draft.enforcement]} Tags are only ever added; a rule never deletes anything or turns
                monitoring off.
              </div>
            </div>
            <div className="space-y-1.5 md:col-span-3">
              <div className="text-sm font-medium">When an asset stops matching</div>
              <div className="flex flex-wrap gap-4 text-sm">
                {((catalog?.on_unmatch?.map((o) => o.key) ?? ['keep', 'revert']) as OnUnmatch[]).map((k) => (
                  <label key={k} className="flex items-center gap-2">
                    <input type="radio" name="on_unmatch" checked={draft.on_unmatch === k} onChange={() => update({ on_unmatch: k })} />
                    {catalog?.on_unmatch?.find((o) => o.key === k)?.label ?? ON_UNMATCH_LABEL[k] ?? k}
                  </label>
                ))}
              </div>
              <div className="text-xs text-muted">{ON_UNMATCH_HELP}</div>
            </div>
          </div>
        </Section>
      </fieldset>

      {(saveError || (showProblems && problems.length > 0)) && (
        <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger-fg">
          {saveError && <div>{saveError}</div>}
          {showProblems && problems.length > 0 && (
            <ul className="list-disc pl-5">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {canManage && (
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => navigate('/discovery/rules')}>
            Cancel
          </Button>
          <Button onClick={onSave} disabled={save.isPending || !catalog}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {isNew ? 'Create rule' : 'Save'}
          </Button>
        </div>
      )}

      {catalog && (
        <PreviewPanel draft={draft} ruleId={isNew ? undefined : id} priority={rule?.priority} catalog={catalog} lookups={lookups} />
      )}
      {catalog && <SampleFacts draft={draft} ruleId={isNew ? undefined : id} priority={rule?.priority} catalog={catalog} lookups={lookups} />}
      {!isNew && id && catalog && <VersionHistory ruleId={id} catalog={catalog} lookups={lookups} />}
    </div>
  )
}

function Section({ title, hint, children }: { title: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{title}</CardTitle>
        {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function ProfileLimit({
  value,
  options,
  onChange,
}: {
  value: string[] | null
  options: RefOption[]
  onChange: (v: string[] | null) => void
}) {
  const limited = value !== null
  const known = new Set(options.map((o) => o.id))
  const unknown = (value || []).filter((v) => !known.has(v))
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-4 text-sm">
        <label className="flex items-center gap-2">
          <input type="radio" checked={!limited} onChange={() => onChange(null)} />
          Results of every discovery profile
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={limited} onChange={() => onChange(value ?? [])} />
          Only these discovery profiles
        </label>
      </div>
      {limited && (
        <div className="flex flex-wrap gap-1.5 rounded-md border border-border bg-surface2/30 p-2">
          {options.length === 0 && unknown.length === 0 && (
            <span className="text-xs text-muted">No discovery profiles to choose from.</span>
          )}
          {options.map((o) => (
            <ToggleChip
              key={o.id}
              active={(value || []).includes(o.id)}
              onClick={() =>
                onChange((value || []).includes(o.id) ? (value || []).filter((x) => x !== o.id) : [...(value || []), o.id])
              }
            >
              {o.name}
            </ToggleChip>
          ))}
          {unknown.map((u) => (
            <ToggleChip key={u} active onClick={() => onChange((value || []).filter((x) => x !== u))}>
              {u.slice(0, 8)}…
            </ToggleChip>
          ))}
        </div>
      )}
    </div>
  )
}

function ToggleChip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60',
        active ? 'border-primary bg-primary/10 text-text' : 'border-border text-muted hover:border-primary/40 hover:text-text',
      )}
    >
      {children}
    </button>
  )
}

// ── condition builder ──────────────────────────────────────────────────────

/** The condition builder, shared with device groups (pages/groups), which pass their own catalog. */
export function GroupEditor({
  root,
  node,
  path,
  catalog,
  options,
  onChange,
  onRemove,
  emptyAllText = 'No conditions: the rule matches every scan result it looks at.',
  kinds,
}: {
  root: ConditionGroup
  node: ConditionGroup
  path: Path
  catalog: RuleCatalog
  options: Record<string, RefOption[]>
  onChange: (root: ConditionGroup) => void
  onRemove?: () => void
  emptyAllText?: string
  /** The rule's asset kinds: the field hints then say which fields controller-managed children lack. */
  kinds?: AssetKind[]
}) {
  const limits = catalog.limits ?? DEFAULT_LIMITS
  const nested = path.length > 0
  return (
    <div className={cn('space-y-2', nested && 'rounded-md border border-border bg-surface2/30 p-3')}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Select value={node.op} onValueChange={(v) => onChange(setGroupOp(root, path, v as GroupOp))}>
          <SelectTrigger className="h-8 w-28" aria-label="Group match">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {catalog.groups.map((g) => (
              <SelectItem key={g.key} value={g.key}>
                {g.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-muted">the following</span>
        {onRemove && (
          <Button variant="ghost" size="sm" className="ml-auto h-7 text-muted hover:text-danger" onClick={onRemove}>
            <Trash2 className="h-3.5 w-3.5" /> Remove group
          </Button>
        )}
      </div>
      {node.conditions.length === 0 && (
        <div className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted">
          {node.op === 'any'
            ? 'An empty “Any of” group matches nothing. Add a condition.'
            : emptyAllText}
        </div>
      )}
      <div className="space-y-2">
        {node.conditions.map((child, i) => {
          const childPath = [...path, i]
          return isGroup(child) ? (
            <GroupEditor
              key={i}
              root={root}
              node={child}
              path={childPath}
              catalog={catalog}
              options={options}
              onChange={onChange}
              onRemove={() => onChange(removeNode(root, childPath))}
              emptyAllText={emptyAllText}
              kinds={kinds}
            />
          ) : (
            <LeafEditor
              key={i}
              leaf={child}
              catalog={catalog}
              options={options}
              kinds={kinds}
              onChange={(leaf) => onChange(updateNode(root, childPath, () => leaf))}
              onRemove={() => onChange(removeNode(root, childPath))}
            />
          )
        })}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!canAddLeaf(root, limits)}
          onClick={() => onChange(addLeaf(root, path, defaultLeaf(catalog), limits))}
        >
          <Plus className="h-3.5 w-3.5" /> Add condition
        </Button>
        {canAddGroup(root, path, limits) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onChange(addGroup(root, path, node.op === 'any' ? 'all' : 'any', defaultLeaf(catalog), limits))}
          >
            <Plus className="h-3.5 w-3.5" /> Add group
          </Button>
        )}
      </div>
    </div>
  )
}

function LeafEditor({
  leaf,
  catalog,
  options,
  kinds,
  onChange,
  onRemove,
}: {
  leaf: ConditionLeaf
  catalog: RuleCatalog
  options: Record<string, RefOption[]>
  kinds?: AssetKind[]
  onChange: (leaf: ConditionLeaf) => void
  onRemove: () => void
}) {
  const field = fieldOf(catalog, leaf.field)
  const op = operatorOf(field, leaf.operator)
  return (
    <div className="flex flex-wrap items-start gap-2 rounded-md border border-border bg-surface p-2">
      <div className="w-full sm:w-52">
        <Select value={field ? leaf.field : undefined} onValueChange={(v) => onChange(changeLeafField(leaf, v, catalog))}>
          <SelectTrigger className="h-9" aria-label="Field">
            <SelectValue placeholder={leaf.field || 'Field'} />
          </SelectTrigger>
          <SelectContent>
            {catalog.fields.map((f) => {
              const hint = fieldSourceHint(f, kinds)
              return (
                <SelectItem key={f.key} value={f.key}>
                  {f.label}
                  {hint && <span className="ml-1.5 text-2xs text-muted">· {hint}</span>}
                </SelectItem>
              )
            })}
          </SelectContent>
        </Select>
        {field?.help && <div className="mt-0.5 px-1 text-2xs text-muted">{field.help}</div>}
      </div>
      <div className="w-full sm:w-48">
        <Select value={op ? leaf.operator : undefined} onValueChange={(v) => onChange(changeLeafOperator(leaf, v, catalog))}>
          <SelectTrigger className="h-9" aria-label="Test">
            <SelectValue placeholder={leaf.operator || 'Test'} />
          </SelectTrigger>
          <SelectContent>
            {(field?.operators || []).map((o) => (
              <SelectItem key={o.key} value={o.key}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="min-w-[12rem] flex-1">
        {field && op && (
          <ValueInput
            kind={op.value}
            field={field}
            operator={op.key}
            value={leaf.value}
            options={field.source ? options[field.source] || [] : []}
            maxRegex={catalog.limits?.max_regex ?? DEFAULT_LIMITS.max_regex}
            onChange={(value) => onChange({ ...leaf, value })}
          />
        )}
      </div>
      <Button variant="ghost" size="icon" className="h-9 w-9 text-muted hover:text-danger" title="Remove condition" onClick={onRemove}>
        <X className="h-4 w-4" />
      </Button>
    </div>
  )
}

/** A comma / newline separated list kept as typed while the parent holds the parsed array. */
function ListInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
}: {
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
  ariaLabel?: string
}) {
  const [text, setText] = useState(() => listText(value))
  useEffect(() => {
    // Only resync when the parsed value changed from outside (field/operator switch, load).
    if (listText(parseList(text)) !== listText(value)) setText(listText(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listText(value)])
  return (
    <Input
      value={text}
      placeholder={placeholder}
      aria-label={ariaLabel}
      onChange={(e) => {
        setText(e.target.value)
        onChange(parseList(e.target.value))
      }}
    />
  )
}

function ValueInput({
  kind,
  field,
  operator,
  value,
  options,
  maxRegex,
  onChange,
}: {
  kind: string
  field: CatalogField
  operator: string
  value: any
  options: RefOption[]
  maxRegex: number
  onChange: (v: any) => void
}) {
  const enumValues = field.type === 'enum' ? field.values || [] : []
  switch (kind) {
    case 'none':
      return <div className="py-2 text-xs text-muted">No value needed</div>
    case 'number':
      return (
        <Input
          type="number"
          aria-label="Value"
          value={Number.isFinite(value) ? value : ''}
          onChange={(e) => onChange(e.target.value === '' ? NaN : Number(e.target.value))}
        />
      )
    case 'range': {
      const [lo, hi] = Array.isArray(value) ? value : [0, 0]
      return (
        <div className="flex items-center gap-2">
          <Input type="number" aria-label="From" value={Number.isFinite(lo) ? lo : ''} onChange={(e) => onChange([e.target.value === '' ? NaN : Number(e.target.value), hi])} />
          <span className="text-xs text-muted">and</span>
          <Input type="number" aria-label="To" value={Number.isFinite(hi) ? hi : ''} onChange={(e) => onChange([lo, e.target.value === '' ? NaN : Number(e.target.value)])} />
        </div>
      )
    }
    case 'cidr_list':
      return <ListInput value={Array.isArray(value) ? value : []} onChange={onChange} ariaLabel="Subnets" placeholder="10.51.0.0/16, 192.168.1.0/24" />
    case 'ip_range':
      return <Input aria-label="Range" value={value ?? ''} placeholder="10.0.0.1-10.0.0.50" onChange={(e) => onChange(e.target.value)} />
    case 'ref':
      return options.length ? (
        <Select value={value || undefined} onValueChange={onChange}>
          <SelectTrigger className="h-9" aria-label="Value">
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {options.map((o) => (
              <SelectItem key={o.id} value={o.id}>
                {o.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <Input aria-label="Id" value={value ?? ''} placeholder="Id (the list could not be loaded)" onChange={(e) => onChange(e.target.value.trim())} />
      )
    case 'ref_list':
      return options.length ? (
        <ChipPicker values={Array.isArray(value) ? value : []} options={options} onChange={onChange} />
      ) : (
        <ListInput value={Array.isArray(value) ? value : []} onChange={onChange} ariaLabel="Ids" placeholder="Ids, comma separated" />
      )
    case 'list':
      return enumValues.length ? (
        <ChipPicker values={Array.isArray(value) ? value : []} options={enumValues.map((v) => ({ id: v, name: v.replace(/_/g, ' ') }))} onChange={onChange} />
      ) : (
        <ListInput
          value={Array.isArray(value) ? value : []}
          onChange={onChange}
          ariaLabel="Values"
          placeholder={field.type === 'set' && field.key === 'open_ports' ? '22, 443, 8443' : 'Comma separated'}
        />
      )
    default:
      if (enumValues.length) {
        return (
          <Select value={value || undefined} onValueChange={onChange}>
            <SelectTrigger className="h-9" aria-label="Value">
              <SelectValue placeholder="Choose…" />
            </SelectTrigger>
            <SelectContent>
              {enumValues.map((v) => (
                <SelectItem key={v} value={v}>
                  {v.replace(/_/g, ' ')}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )
      }
      return (
        <Input
          aria-label="Value"
          value={value ?? ''}
          maxLength={operator === 'matches' ? maxRegex : undefined}
          className={field.type === 'oid' || operator === 'matches' ? 'font-mono text-xs' : undefined}
          placeholder={
            operator === 'matches' ? 'Regular expression' : field.type === 'oid' ? '1.3.6.1.4.1.12356' : field.type === 'version' ? '7.2.5' : ''
          }
          onChange={(e) => onChange(e.target.value)}
        />
      )
  }
}

function ChipPicker({ values, options, onChange }: { values: string[]; options: RefOption[]; onChange: (v: string[]) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5 py-1">
      {options.map((o) => (
        <ToggleChip
          key={o.id}
          active={values.includes(o.id)}
          onClick={() => onChange(values.includes(o.id) ? values.filter((x) => x !== o.id) : [...values, o.id])}
        >
          {o.name}
        </ToggleChip>
      ))}
    </div>
  )
}

// ── actions ─────────────────────────────────────────────────────────────────

function ActionsEditor({
  draft,
  catalog,
  options,
  lookups,
  can,
  onChange,
}: {
  draft: RuleDraft
  catalog: RuleCatalog
  options: Record<string, RefOption[]>
  lookups: Lookups
  can: (p: string) => boolean
  onChange: (actions: RuleAction[]) => void
}) {
  const withChildren = draft.asset_kinds.includes('child')
  const available = pickableActions(catalog, draft.asset_kinds, draft.actions.map((a) => a.key))
  const ordered = [...draft.actions].sort((a, b) => (a.key === 'decision' ? -1 : b.key === 'decision' ? 1 : 0))
  const set = (key: string, next: RuleAction) => onChange(draft.actions.map((a) => (a.key === key ? next : a)))
  const issues = actionProblems(draft, catalog)

  return (
    <div className="space-y-3">
      {ordered.length === 0 && <div className="text-sm text-muted">No actions yet: add at least one.</div>}
      <div className="grid gap-3 lg:grid-cols-2">
        {ordered.map((a) => {
          const spec = catalog.actions.find((s) => s.key === a.key)
          const notApplicable = spec && !spec.asset_kinds.some((k) => draft.asset_kinds.includes(k))
          const notForChildren = !!spec && !notApplicable && withChildren && !spec.asset_kinds.includes('child')
          const missing = spec ? activationPermissions([a], draft.asset_kinds, catalog).filter((p) => !can(p)) : []
          return (
            <div key={a.key} className={cn('rounded-md border bg-surface p-3', a.key === 'decision' ? 'border-primary/40 lg:col-span-2' : 'border-border')}>
              <div className="mb-2 flex items-start justify-between gap-2">
                <div>
                  <div className="text-sm font-medium">{spec?.label ?? a.key}</div>
                  {spec?.help && <div className="text-xs text-muted">{spec.help}</div>}
                  {notApplicable && (
                    <div className="text-xs text-warning-fg">Applies to {kindsPhrase(spec!.asset_kinds)} only.</div>
                  )}
                  {notForChildren && (
                    <div className="text-xs text-muted">Not applied to controller-managed switches and access points.</div>
                  )}
                  {missing.length > 0 && (
                    <div className="text-xs text-warning-fg">
                      Activating needs {missing.join(', ')}, which your role lacks.
                    </div>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-muted hover:text-danger"
                  title="Remove action"
                  onClick={() => onChange(draft.actions.filter((x) => x.key !== a.key))}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
              {spec ? (
                <ActionValue action={a} spec={spec} catalog={catalog} options={options} lookups={lookups} onChange={(next) => set(a.key, next)} />
              ) : (
                <div className="text-xs text-muted">{summarizeAction(a)}</div>
              )}
            </div>
          )
        })}
      </div>
      {withChildren && (
        <div className="text-xs text-muted">
          This rule includes controller-managed switches and access points, so only the actions that apply to them are
          offered.
        </div>
      )}
      {available.length > 0 && (
        <Select value="" onValueChange={(key) => {
          const spec = catalog.actions.find((s) => s.key === key)
          if (spec) onChange([...draft.actions, defaultActionValue(spec)])
        }}>
          <SelectTrigger className="h-9 w-64" aria-label="Add action">
            <span className="flex items-center gap-1.5 text-sm"><Plus className="h-3.5 w-3.5" /> Add action</span>
          </SelectTrigger>
          <SelectContent>
            {available.map((s) => (
              <SelectItem key={s.key} value={s.key}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {issues.length > 0 && draft.actions.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-warning-fg">
          {issues.map((i) => (
            <li key={i}>{i}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ActionValue({
  action,
  spec,
  catalog,
  options,
  lookups,
  onChange,
}: {
  action: RuleAction
  spec: CatalogAction
  catalog: RuleCatalog
  options: Record<string, RefOption[]>
  lookups: Lookups
  onChange: (a: RuleAction) => void
}) {
  if (spec.source === 'devices' || spec.key === 'dependency_parent') {
    return <DevicePicker value={action.value || ''} lookups={lookups} label={spec.label} onChange={(v) => onChange({ ...action, value: v })} />
  }
  switch (spec.value) {
    case 'decision':
      return (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {(catalog.decisions || ['auto_import', 'review', 'ignore']).map((d) => (
              <ToggleChip
                key={d}
                active={action.value === d}
                onClick={() => onChange(d === 'auto_import' ? { key: action.key, value: d, import_as: action.import_as || 'device' } : { key: action.key, value: d })}
              >
                {DECISION_LABEL[d] ?? d}
              </ToggleChip>
            ))}
          </div>
          {action.value === 'auto_import' && (
            <FormField label="Import as" className="max-w-sm">
              <Select value={action.import_as || 'device'} onValueChange={(v) => onChange({ ...action, import_as: v as any })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(catalog.import_as || ['device', 'server', 'both', 'auto']).map((k) => (
                    <SelectItem key={k} value={k}>
                      {k === 'auto' ? 'Auto-route (servers → Servers, gear → Devices)' : sentenceCase(IMPORT_AS_LABEL[k] ?? k)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
          )}
        </div>
      )
    case 'ref': {
      const list = spec.source ? options[spec.source] || [] : []
      return list.length ? (
        <Select value={action.value || undefined} onValueChange={(v) => onChange({ ...action, value: v })}>
          <SelectTrigger aria-label={spec.label}>
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {list.map((o) => (
              <SelectItem key={o.id} value={o.id}>
                {o.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <div className="space-y-1">
          <Input aria-label={spec.label} value={action.value ?? ''} placeholder="Id (the list could not be loaded)" onChange={(e) => onChange({ ...action, value: e.target.value.trim() })} />
          {spec.source === 'manual_maps' && (
            <div className="text-2xs text-muted">No network map could be listed: create one under Network Maps, or ask for map access.</div>
          )}
        </div>
      )
    }
    case 'int':
      return (
        <Input
          type="number"
          aria-label={spec.label}
          min={spec.min ?? undefined}
          max={spec.max ?? undefined}
          value={Number.isFinite(Number(action.value)) ? action.value : ''}
          onChange={(e) => onChange({ ...action, value: e.target.value === '' ? '' : Math.trunc(Number(e.target.value)) })}
        />
      )
    case 'on':
      return (
        <div className="flex items-center gap-2">
          <Badge variant="success">On</Badge>
          <span className="text-xs text-muted">Turns it on where it is off; never turns it off.</span>
        </div>
      )
    case 'tags':
      return (
        <ListInput
          value={Array.isArray(action.value) ? action.value : []}
          onChange={(v) => onChange({ ...action, value: v })}
          ariaLabel={spec.label}
          placeholder="datacenter, firewall"
        />
      )
    default:
      if (spec.key === 'device_type' && catalog.device_types?.length) {
        return (
          <Select value={action.value || undefined} onValueChange={(v) => onChange({ ...action, value: v })}>
            <SelectTrigger aria-label={spec.label}>
              <SelectValue placeholder="Choose…" />
            </SelectTrigger>
            <SelectContent>
              {catalog.device_types.map((t) => (
                <SelectItem key={t} value={t}>
                  {t.replace(/_/g, ' ')}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )
      }
      return <Input aria-label={spec.label} value={action.value ?? ''} maxLength={255} onChange={(e) => onChange({ ...action, value: e.target.value })} />
  }
}

function ActivationNote({
  actions,
  assetKinds,
  catalog,
  can,
}: {
  actions: RuleAction[]
  assetKinds: AssetKind[]
  catalog: RuleCatalog
  can: (p: string) => boolean
}) {
  const needed = activationPermissions(actions, assetKinds, catalog)
  const missing = needed.filter((p) => !can(p))
  if (!needed.length) return null
  return (
    <div className={cn('text-xs md:col-span-3', missing.length ? 'text-warning-fg' : 'text-muted')}>
      Activating this rule needs {needed.join(', ')}.
      {missing.length > 0 && ` Your role lacks ${missing.join(', ')}: save it as a draft and ask someone who has it to activate it.`}
    </div>
  )
}

// ── preview ─────────────────────────────────────────────────────────────────

function previewRule(draft: RuleDraft, ruleId?: string, priority?: number) {
  const body = ruleBody(draft)
  return { ...body, name: body.name || 'Unsaved rule', ...(ruleId ? { id: ruleId } : {}), ...(priority != null ? { priority } : {}) }
}

function PreviewPanel({
  draft,
  ruleId,
  priority,
  catalog,
  lookups,
}: {
  draft: RuleDraft
  ruleId?: string
  priority?: number
  catalog: RuleCatalog
  lookups: Lookups
}) {
  const { data: runs = [] } = useQuery<DiscoveryRun[]>({
    queryKey: ['discovery', 'runs'],
    queryFn: () => discoveryApi.listRuns(),
  })
  const finished = runs.filter((r) => r.status !== 'queued' && r.status !== 'running')
  const [runId, setRunId] = useState<string>('')
  const [scope, setScope] = useState<'with_active' | 'only'>('with_active')
  const [mode, setMode] = useState<'profile' | ImportMode>('profile')
  const [show, setShow] = useState<'matched' | 'all'>('matched')
  const [result, setResult] = useState<PreviewResponse | null>(null)
  const selectedRun = runId || finished[0]?.id || ''
  const blockers = useMemo(() => [...conditionProblems(draft.condition, catalog, catalog.limits), ...actionProblems(draft, catalog)], [draft, catalog])

  const preview = useMutation({
    mutationFn: () =>
      rulesApi.preview({
        rule: previewRule(draft, ruleId, priority),
        scope,
        discovery_run_id: selectedRun,
        ...(mode !== 'profile' ? { import_mode: mode } : {}),
      }),
    onSuccess: setResult,
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const rows = (result?.rows || []).filter((r) => show === 'all' || (r.plan?.matched?.length ?? 0) > 0)
  const shown = rows.slice(0, 500)

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Play className="h-4 w-4 text-primary" /> Preview against a past scan
        </CardTitle>
        <p className="mt-1 text-xs text-muted">Shows what the rules would do to that scan's results. Nothing is imported or changed.</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 md:grid-cols-4">
          <FormField label="Scan" className="md:col-span-2">
            <Select value={selectedRun || undefined} onValueChange={setRunId}>
              <SelectTrigger>
                <SelectValue placeholder={finished.length ? 'Choose a scan' : 'No finished scans yet'} />
              </SelectTrigger>
              <SelectContent>
                {finished.slice(0, 100).map((r) => (
                  <SelectItem key={r.id} value={r.id}>
                    {r.profile_name || 'Scan'} · {regionalDate(new Date(r.started_at || r.created_at), 'datetime')} ·{' '}
                    {r.responding_targets} found
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Rules">
            <Select value={scope} onValueChange={(v) => setScope(v as 'with_active' | 'only')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="with_active">This rule among the active rules</SelectItem>
                <SelectItem value="only">This rule only</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Profile import mode">
            <Select value={mode} onValueChange={(v) => setMode(v as any)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="profile">As the scan's profile is set</SelectItem>
                <SelectItem value="auto_match">What if: rules may auto-import and ignore</SelectItem>
                <SelectItem value="ignore_match">What if: rules may only ignore</SelectItem>
                <SelectItem value="review">What if: rules only annotate</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => preview.mutate()} disabled={!selectedRun || preview.isPending || blockers.length > 0}>
            {preview.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            Run preview
          </Button>
          {blockers.length > 0 && <span className="text-xs text-warning-fg">Fix the rule first: {blockers[0]}</span>}
        </div>

        {result && (
          <div className="space-y-2">
            <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-sm">
              <span className="font-medium">{sentenceCase(summarizePreview(result.summary, result.license_room))}</span>
              <div className="mt-0.5 text-xs text-muted">
                {result.profile_name ? `${result.profile_name} · ` : ''}
                {IMPORT_MODE_TEXT[result.import_mode]?.label ?? result.import_mode} · up to {result.max_auto_import_per_run}{' '}
                auto-imports per scan
                {result.summary.no_match ? ` · ${result.summary.no_match} matched no rule` : ''}
              </div>
            </div>
            <div className="flex items-center gap-1">
              {(['matched', 'all'] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setShow(k)}
                  className={cn(
                    'rounded-md px-2.5 py-1 text-xs font-medium',
                    show === k ? 'bg-primary/10 text-text' : 'text-muted hover:bg-surface2 hover:text-text',
                  )}
                >
                  {k === 'matched' ? 'Matched' : 'All evaluated'}
                </button>
              ))}
              <span className="ml-auto text-xs text-muted">
                {rows.length} row{rows.length === 1 ? '' : 's'}
                {rows.length > shown.length ? ` (first ${shown.length} shown)` : ''}
              </span>
            </div>
            {shown.length === 0 ? (
              <div className="py-6 text-center text-sm text-muted">
                {show === 'matched' ? 'No result in this scan matches.' : 'The scan has no results to evaluate.'}
              </div>
            ) : (
              <div className="rounded-md border border-border">
                <Table>
                  <THead className="bg-surface2/50">
                    <Tr>
                      <Th>IP</Th>
                      <Th>Hostname</Th>
                      <Th>Vendor / Model</Th>
                      <Th>Decision</Th>
                      <Th>Settings</Th>
                      <Th className="w-16"></Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {shown.map((r, i) => (
                      <Tr key={r.result_id ?? i}>
                        <Td className="font-mono text-xs">{r.ip_address}</Td>
                        <Td className="text-xs">{r.hostname || '—'}</Td>
                        <Td className="text-xs">
                          {r.vendor || '—'}
                          {r.model && <div className="text-2xs text-muted">{r.model}</div>}
                        </Td>
                        <Td>
                          <DispositionBadge disposition={r.disposition} reason={r.reason} />
                        </Td>
                        <Td>
                          <PlanChips plan={r.plan} catalog={catalog} lookups={lookups} />
                        </Td>
                        <Td>
                          <PlanWhy plan={r.plan} catalog={catalog} lookups={lookups} />
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function PlanChips({ plan, catalog, lookups }: { plan: PreviewResponse['rows'][number]['plan']; catalog: RuleCatalog; lookups: Lookups }) {
  const chips = Object.entries(plan?.settings || {})
    .filter(([k]) => k !== 'decision')
    .map(([key, s]) => summarizeAction({ key, value: s.value }, lookups, catalog))
  if (plan?.tags?.length) chips.push(`Tags: ${plan.tags.map((t) => t.value).join(', ')}`)
  if (!chips.length) return <span className="text-xs text-muted">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {chips.map((c, i) => (
        <Badge key={i} variant="outline">
          {c}
        </Badge>
      ))}
    </div>
  )
}

const SAMPLE_FIELDS: { key: string; label: string; placeholder: string }[] = [
  { key: 'ip_address', label: 'IP address', placeholder: '10.51.2.12' },
  { key: 'sys_object_id', label: 'sysObjectID', placeholder: '1.3.6.1.4.1.12356.101.1.60' },
  { key: 'vendor', label: 'Vendor', placeholder: 'Fortinet' },
  { key: 'sys_descr', label: 'sysDescr', placeholder: 'FortiGate-60F v7.2.5' },
  { key: 'open_ports', label: 'Open ports', placeholder: '22, 443' },
]

function SampleFacts({
  draft,
  ruleId,
  priority,
  catalog,
  lookups,
}: {
  draft: RuleDraft
  ruleId?: string
  priority?: number
  catalog: RuleCatalog
  lookups: Lookups
}) {
  const [open, setOpen] = useState(false)
  const [facts, setFacts] = useState<Record<string, string>>({})
  const [scope, setScope] = useState<'with_active' | 'only'>('only')
  const [result, setResult] = useState<PreviewResponse | null>(null)
  const known = new Set(catalog.fields.map((f) => f.key))
  const fields = SAMPLE_FIELDS.filter((f) => known.has(f.key))

  const run = useMutation({
    mutationFn: () => {
      const clean: Record<string, any> = {}
      for (const [k, v] of Object.entries(facts)) if (v.trim()) clean[k] = v.trim()
      return rulesApi.preview({ rule: previewRule(draft, ruleId, priority), scope, facts: clean })
    },
    onSuccess: setResult,
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })
  const row = result?.rows?.[0]

  return (
    <Card>
      <CardHeader className="pb-3">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <CardTitle className="flex items-center gap-2 text-base">
            <FlaskConical className="h-4 w-4 text-primary" /> Test with sample facts
          </CardTitle>
        </button>
      </CardHeader>
      {open && (
        <CardContent className="space-y-3">
          <p className="text-xs text-muted">
            Type what a scan might find about one device and see which rules match. Empty fields count as unknown.
          </p>
          <div className="grid gap-3 md:grid-cols-3">
            {fields.map((f) => (
              <FormField key={f.key} label={f.label}>
                <Input
                  value={facts[f.key] ?? ''}
                  placeholder={f.placeholder}
                  className={f.key === 'sys_object_id' ? 'font-mono text-xs' : undefined}
                  onChange={(e) => setFacts({ ...facts, [f.key]: e.target.value })}
                />
              </FormField>
            ))}
            <FormField label="Rules">
              <Select value={scope} onValueChange={(v) => setScope(v as 'with_active' | 'only')}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="only">This rule only</SelectItem>
                  <SelectItem value="with_active">This rule among the active rules</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
          </div>
          <Button variant="outline" onClick={() => run.mutate()} disabled={run.isPending}>
            {run.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />}
            Test
          </Button>
          {row && (
            <div className="space-y-2 rounded-md border border-border bg-surface2/30 p-3">
              <div className="flex items-center gap-2 text-sm">
                <span className="font-medium">Result:</span>
                <DispositionBadge disposition={row.disposition} reason={row.reason} />
                <span className="text-xs text-muted">(as if the profile let rules auto-import and ignore)</span>
              </div>
              <PlanDetails plan={row.plan} catalog={catalog} lookups={lookups} />
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}

// ── versions ────────────────────────────────────────────────────────────────

function VersionHistory({ ruleId, catalog, lookups }: { ruleId: string; catalog: RuleCatalog; lookups: Lookups }) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const { data: versions = [], isLoading } = useQuery<RuleVersion[]>({
    queryKey: ['discovery', 'rules', 'versions', ruleId],
    queryFn: () => rulesApi.versions(ruleId),
    enabled: open,
  })
  return (
    <Card>
      <CardHeader className="pb-3">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <CardTitle className="flex items-center gap-2 text-base">
            <History className="h-4 w-4 text-primary" /> Version history
          </CardTitle>
        </button>
      </CardHeader>
      {open && (
        <CardContent>
          {isLoading ? (
            <div className="py-4 text-center text-sm text-muted">Loading…</div>
          ) : versions.length === 0 ? (
            <div className="py-4 text-center text-sm text-muted">No saved versions.</div>
          ) : (
            <ul className="divide-y divide-border">
              {versions.map((v) => {
                const snap = v.snapshot || {}
                const isOpen = expanded === v.revision
                return (
                  <li key={v.revision} className="py-2">
                    <button
                      type="button"
                      className="flex w-full flex-wrap items-center gap-2 text-left text-sm"
                      onClick={() => setExpanded(isOpen ? null : v.revision)}
                      aria-expanded={isOpen}
                    >
                      {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      <span className="font-medium">Revision {v.revision}</span>
                      {v.reason && <Badge variant="outline">{v.reason}</Badge>}
                      <span className="text-xs text-muted">
                        {v.changed_by_name || 'someone'} · {regionalDate(new Date(v.changed_at), 'datetime')}
                      </span>
                    </button>
                    {isOpen && (
                      <div className="ml-6 mt-1 space-y-1 text-xs">
                        {snap.name && <div><span className="text-muted">Name:</span> {snap.name}</div>}
                        {snap.mode && <div><span className="text-muted">Mode:</span> {snap.mode}</div>}
                        {snap.condition && (
                          <div><span className="text-muted">If:</span> {summarizeCondition(snap.condition, catalog, lookups)}</div>
                        )}
                        {Array.isArray(snap.actions) && (
                          <div><span className="text-muted">Then:</span> {summarizeActions(snap.actions, lookups, catalog).join(' · ')}</div>
                        )}
                        {snap.stop_processing && <div className="text-muted">Stops further rules</div>}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      )}
    </Card>
  )
}
