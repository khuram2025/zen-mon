import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Clock,
  Copy,
  Download,
  GitCompare,
  ListChecks,
  Loader2,
  Pencil,
  Plus,
  Play,
  Power,
  SearchX,
  Settings2,
  Sparkles,
  Trash2,
  Undo2,
  Upload,
  X,
} from 'lucide-react'
import { regionalDate } from '@/lib/regionalTime'
import { useCan } from '@/stores/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { rulesApi } from './api'
import type {
  ApprovalResult,
  AssetKind,
  ConflictReport,
  DiscoveryRule,
  EngineRun,
  PendingChanges,
  RuleCatalog,
  RuleEngineSettings,
  RuleMode,
  RuleScheduleStatus,
  RulesImportResult,
  ScheduleRunResult,
  StarterRule,
  UndoResult,
  UnmatchedReport,
} from './types'
import {
  ASSET_KIND_ORDER,
  ASSET_KIND_SHORT,
  SCHEDULE_HOURS,
  browserTimeZone,
  engineSettingsProblems,
  itemOutcomeLabel,
  scheduleEveryText,
  scheduleRunText,
  scheduleText,
  untilText,
  unmatchedKindsText,
  CHANGE_OUTCOME_LABEL,
  CHANGE_OUTCOME_ORDER,
  TRIGGER_REASON,
  dependencyIds,
  exportFileName,
  parseRulesFile,
  pendingCount,
  runStatusLabel,
  summarizeApprovalResult,
  summarizeInventoryPreview,
  waitingText,
  Lookups,
  MODE_LABEL,
  MODE_ORDER,
  OUTCOME_LABEL,
  OUTCOME_TONE,
  assetPath,
  canUndoRun,
  changeText,
  plural,
  ruleRunsText,
  runStatusTone,
  runTriggerText,
  settingLabel,
  summarizeUndoResult,
  moveItem,
  summarizeActions,
  sentenceCase,
  summarizeCondition,
  summarizeCounts,
} from './ruleModel'
import { ChangesTable, ModeBadge, useRuleCatalog, useRuleLookups, useWithDeviceNames } from './ruleUi'

const RULES_KEY = ['discovery', 'rules', 'list'] as const
const SETTINGS_KEY = ['discovery', 'rules', 'settings'] as const
const HISTORY_KEY = ['discovery', 'rules', 'history'] as const
const SCHEDULE_KEY = ['discovery', 'rules', 'schedule'] as const

/** The recent runs, shared by the waiting banner and the history card (one request). */
function useRuleHistory() {
  return useQuery<EngineRun[]>({
    queryKey: HISTORY_KEY,
    queryFn: () => rulesApi.history({ limit: 25 }),
    refetchInterval: 30_000,
  })
}

export function RulesPage() {
  const can = useCan()
  const canManage = can('discovery.manage_rules')
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data: catalog } = useRuleCatalog()
  const { lookups: baseLookups } = useRuleLookups()
  const [deleting, setDeleting] = useState<DiscoveryRule | null>(null)
  const [params, setParams] = useSearchParams()
  const reviewId = params.get('review')
  const [starterOpen, setStarterOpen] = useState(false)

  const { data: rules = [], isLoading } = useQuery<DiscoveryRule[]>({
    queryKey: RULES_KEY,
    queryFn: rulesApi.list,
  })
  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(rules.flatMap((r) => r.actions || [])), [rules]))
  const { data: runs = [] } = useRuleHistory()
  const waiting = runs.filter((r) => r.status === 'needs_approval')
  const openReview = (id: string | null) => {
    const next = new URLSearchParams(params)
    if (id) next.set('review', id)
    else next.delete('review')
    setParams(next, { replace: !id })
  }

  const setRules = (rows: DiscoveryRule[]) => qc.setQueryData(RULES_KEY, rows)
  const refresh = () => qc.invalidateQueries({ queryKey: RULES_KEY })

  const reorder = useMutation({
    mutationFn: (ids: string[]) => rulesApi.reorder(ids),
    onSuccess: setRules,
    onError: (e: any) => {
      toast.error('Could not reorder', apiErrorMessage(e))
      refresh()
    },
  })
  const setMode = useMutation({
    mutationFn: ({ id, mode }: { id: string; mode: RuleMode }) => rulesApi.setMode(id, mode),
    onSuccess: (r) => {
      refresh()
      toast.success(`${r.name}: ${MODE_LABEL[r.mode as RuleMode] ?? r.mode}`)
    },
    onError: (e: any) => toast.error('Could not change the mode', apiErrorMessage(e)),
  })
  const clone = useMutation({
    mutationFn: (id: string) => rulesApi.clone(id),
    onSuccess: (r) => {
      refresh()
      toast.success('Rule copied as a draft', r.name)
    },
    onError: (e: any) => toast.error('Could not copy the rule', apiErrorMessage(e)),
  })
  const remove = useMutation({
    mutationFn: (id: string) => rulesApi.remove(id),
    onSuccess: () => {
      refresh()
      setDeleting(null)
      toast.success('Rule deleted')
    },
    onError: (e: any) => toast.error('Could not delete the rule', apiErrorMessage(e)),
  })

  const move = (index: number, delta: number) => {
    const ids = rules.map((r) => r.id)
    const next = moveItem(ids, index, delta)
    if (next === ids) return
    // Show the new order at once; the server answer replaces it.
    setRules(next.map((id) => rules.find((r) => r.id === id)!))
    reorder.mutate(next)
  }

  return (
    <div className="space-y-4">
      {waiting.length > 0 && (
        <div
          role="status"
          className="flex flex-wrap items-center gap-3 rounded-md border border-warning/50 bg-warning/10 px-4 py-3 text-sm text-warning-fg"
        >
          <ClipboardCheck className="h-5 w-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="font-semibold">{waitingText(runs)}</div>
            <div className="text-xs">
              An automatic run (a re-check after changes or a scheduled run) reached a limit, so it waits for someone to
              review it. Nothing has been changed yet.
            </div>
          </div>
          {reviewId !== waiting[0].id && (
            <Button size="sm" variant="outline" onClick={() => openReview(waiting[0].id)}>
              {canManage ? 'Review' : 'View changes'}
            </Button>
          )}
        </div>
      )}

      {reviewId && (
        <ReviewPanel
          key={reviewId}
          runId={reviewId}
          run={runs.find((r) => r.id === reviewId)}
          canManage={canManage}
          catalog={catalog}
          lookups={baseLookups}
          onClose={() => openReview(null)}
        />
      )}

      <EngineCard
        canManage={canManage}
        activeCount={rules.filter((r) => r.mode === 'active').length}
        catalog={catalog}
        onReview={openReview}
      />

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm">
              <ListChecks className="h-4 w-4 text-primary" />
              Rules
            </CardTitle>
            <p className="mt-1 text-xs text-muted">
              Evaluated top to bottom on scan results, on new devices and servers, and when you run them on inventory.
              The first matching rule sets each setting; tags from every matching rule are merged. Rules never delete
              anything or turn monitoring off, except interfaces an ignore pattern names.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <ExportButton disabled={rules.length === 0} />
            {canManage && (
              <>
                <ImportButton />
                <Button size="sm" variant="outline" onClick={() => setStarterOpen(true)}>
                  <Sparkles className="h-4 w-4" /> Add starter rules
                </Button>
                <Button size="sm" variant="outline" onClick={() => navigate('/discovery/rules/run')}>
                  <Play className="h-4 w-4" /> Run on inventory
                </Button>
                <Button size="sm" onClick={() => navigate('/discovery/rules/new')}>
                  <Plus className="h-4 w-4" /> New rule
                </Button>
              </>
            )}
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="py-12 text-center text-sm text-muted">Loading…</div>
          ) : rules.length === 0 ? (
            <div className="flex flex-col items-center gap-3 py-14 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
                <ListChecks className="h-7 w-7 text-primary" />
              </div>
              <h3 className="text-lg font-semibold">No discovery rules yet</h3>
              <p className="max-w-md text-sm text-muted">
                Rules decide what happens to what a scan finds: import it, keep it for review or ignore it, and which
                group, template, credential and tags a new device gets.
              </p>
              {canManage && (
                <div className="flex flex-wrap justify-center gap-2">
                  <Button variant="outline" onClick={() => setStarterOpen(true)}>
                    <Sparkles className="h-4 w-4" /> Add starter rules
                  </Button>
                  <Button onClick={() => navigate('/discovery/rules/new')}>
                    <Plus className="h-4 w-4" /> New rule
                  </Button>
                </div>
              )}
            </div>
          ) : (
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-20">Order</Th>
                  <Th className="w-32">Mode</Th>
                  <Th>Rule</Th>
                  <Th>Actions</Th>
                  <Th className="text-right">Matched last scan</Th>
                  <Th className="w-28"></Th>
                </Tr>
              </THead>
              <TBody>
                {rules.map((r, i) => (
                  <Tr key={r.id} className={r.mode === 'disabled' ? 'opacity-70' : ''}>
                    <Td>
                      <div className="flex items-center gap-0.5">
                        <span className="w-5 text-xs tabular-nums text-muted">{i + 1}</span>
                        {canManage && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7"
                              title="Move up"
                              aria-label={`Move ${r.name} up`}
                              disabled={i === 0 || reorder.isPending}
                              onClick={() => move(i, -1)}
                            >
                              <ArrowUp className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7"
                              title="Move down"
                              aria-label={`Move ${r.name} down`}
                              disabled={i === rules.length - 1 || reorder.isPending}
                              onClick={() => move(i, 1)}
                            >
                              <ArrowDown className="h-3.5 w-3.5" />
                            </Button>
                          </>
                        )}
                      </div>
                    </Td>
                    <Td>
                      {canManage ? (
                        <Select
                          value={r.mode}
                          onValueChange={(v) => v !== r.mode && setMode.mutate({ id: r.id, mode: v as RuleMode })}
                        >
                          <SelectTrigger className="h-8 w-28 text-xs" aria-label={`Mode of ${r.name}`}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {MODE_ORDER.map((m) => (
                              <SelectItem key={m} value={m}>
                                {MODE_LABEL[m]}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      ) : (
                        <ModeBadge mode={r.mode} />
                      )}
                    </Td>
                    <Td className="max-w-md">
                      <Link to={`/discovery/rules/${r.id}`} className="font-medium hover:underline">
                        {r.name}
                      </Link>
                      <div className="text-xs text-muted">
                        {catalog ? sentenceCase(summarizeCondition(r.condition, catalog, lookups)) : '…'}
                      </div>
                      <div className="mt-0.5 text-2xs text-muted">
                        {r.asset_kinds.map((k) => ASSET_KIND_SHORT[k as AssetKind] ?? k).join(' + ')}
                        {r.profile_ids?.length
                          ? ` · ${r.profile_ids.map((p) => lookups.discovery_profiles?.[p] ?? 'profile').join(', ')} only`
                          : ''}
                        {r.stop_processing ? ' · stops further rules' : ''}
                      </div>
                      <div className="text-2xs text-muted">{sentenceCase(ruleRunsText(r))}</div>
                    </Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {summarizeActions(r.actions, lookups, catalog).map((t, k) => (
                          <Badge key={k} variant={k === 0 && r.actions.some((a) => a.key === 'decision') ? 'info' : 'outline'}>
                            {t}
                          </Badge>
                        ))}
                      </div>
                    </Td>
                    <Td className="text-right text-xs tabular-nums">
                      {r.last_match_count != null ? r.last_match_count : '—'}
                      {r.last_evaluated_at && (
                        <div className="text-2xs text-muted">{relativeTime(r.last_evaluated_at)}</div>
                      )}
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          title={canManage ? 'Edit' : 'View'}
                          onClick={() => navigate(`/discovery/rules/${r.id}`)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        {canManage && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              title="Copy as draft"
                              disabled={clone.isPending}
                              onClick={() => clone.mutate(r.id)}
                            >
                              <Copy className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8 text-muted hover:text-danger"
                              title="Delete"
                              onClick={() => setDeleting(r)}
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </>
                        )}
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <ConflictsCard catalog={catalog} />

      <UnmatchedCard />

      <HistoryCard canManage={canManage} catalog={catalog} lookups={baseLookups} onReview={openReview} />

      {canManage && <StarterDialog open={starterOpen} onOpenChange={setStarterOpen} />}

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete this rule?"
        description={
          deleting ? (
            <>
              <span className="font-medium">{deleting.name}</span> stops taking part in scans. Past rule runs keep its
              name in their history.
            </>
          ) : undefined
        }
        confirmText="Delete"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting.id)
        }}
      />
    </div>
  )
}


function EngineCard({
  canManage,
  activeCount,
  catalog,
  onReview,
}: {
  canManage: boolean
  activeCount: number
  catalog: RuleCatalog | undefined
  onReview: (runId: string) => void
}) {
  const qc = useQueryClient()
  const { data: settings } = useQuery<RuleEngineSettings>({ queryKey: SETTINGS_KEY, queryFn: rulesApi.getSettings })
  const [form, setForm] = useState<RuleEngineSettings | null>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (settings) setForm(settings)
  }, [settings])

  const save = useMutation({
    mutationFn: (s: RuleEngineSettings) => rulesApi.putSettings(s),
    onSuccess: (s) => {
      qc.setQueryData(SETTINGS_KEY, s)
      qc.invalidateQueries({ queryKey: SCHEDULE_KEY })
      toast.success(s.enabled ? 'Discovery rules are on' : 'Discovery rules are off')
    },
    onError: (e: any) => toast.error('Could not save the settings', apiErrorMessage(e)),
  })

  if (!settings || !form) {
    return (
      <Card>
        <CardContent className="py-6 text-center text-sm text-muted">Loading…</CardContent>
      </Card>
    )
  }

  const on = settings.enabled
  const hours = catalog?.schedule_hours?.length ? catalog.schedule_hours : SCHEDULE_HOURS
  const PHASE4_KEYS = [
    'growth_breaker_pct',
    'growth_breaker_min',
    'schedule_enabled',
    'schedule_every_hours',
    'schedule_time',
    'schedule_timezone',
  ] as const
  const dirty =
    form.max_auto_import_per_run !== settings.max_auto_import_per_run ||
    form.license_reserve_nodes !== settings.license_reserve_nodes ||
    form.history_days !== settings.history_days ||
    form.max_changes_per_run !== settings.max_changes_per_run ||
    PHASE4_KEYS.some((k) => form[k] !== settings[k])
  const phase4Problems = engineSettingsProblems(form, hours)
  const valid =
    Number.isInteger(form.max_auto_import_per_run) && form.max_auto_import_per_run >= 1 && form.max_auto_import_per_run <= 1000 &&
    Number.isInteger(form.license_reserve_nodes) && form.license_reserve_nodes >= 0 &&
    Number.isInteger(form.history_days) && form.history_days >= 7 && form.history_days <= 3650 &&
    Number.isInteger(form.max_changes_per_run) && form.max_changes_per_run >= 1 && form.max_changes_per_run <= 100000 &&
    phase4Problems.length === 0
  const growthPct = settings.growth_breaker_pct ?? 50

  return (
    <Card className={cn(on ? 'border-success/40' : 'border-warning/40')}>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-start gap-3">
            <div
              className={cn(
                'mt-0.5 flex h-8 w-8 items-center justify-center rounded-full',
                on ? 'bg-success/10 text-success-fg' : 'bg-warning/10 text-warning-fg',
              )}
            >
              <Power className="h-4 w-4" />
            </div>
            <div>
              <div className="text-sm font-semibold">Discovery rules are {on ? 'on' : 'off'}</div>
              <p className="text-xs text-muted">
                {on
                  ? `${activeCount} active rule${activeCount === 1 ? '' : 's'} run at the end of every scan. A profile's import mode decides whether they may import or ignore, or only annotate.`
                  : 'Scans ignore rules: nothing is annotated, imported or ignored. You can still build rules and preview them against past scans.'}
              </p>
              <p className="mt-0.5 text-2xs text-muted">
                Up to {settings.max_auto_import_per_run} auto-imports per scan · {settings.license_reserve_nodes} license
                node{settings.license_reserve_nodes === 1 ? '' : 's'} kept free · history kept {settings.history_days} days
                {settings.max_changes_per_run != null &&
                  ` · an automatic run changing more than ${settings.max_changes_per_run} values waits for approval`}
                {growthPct > 0
                  ? ` · growth stop at ${growthPct} % (at least ${settings.growth_breaker_min ?? 20} assets)`
                  : ' · growth stop off'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {canManage && (
              <Button variant="ghost" size="sm" onClick={() => setOpen(!open)} aria-expanded={open}>
                <Settings2 className="h-4 w-4" /> Settings
              </Button>
            )}
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted">{on ? 'On' : 'Off'}</span>
              <Switch
                checked={on}
                disabled={!canManage || save.isPending}
                aria-label="Run discovery rules on scans"
                onCheckedChange={(v) => save.mutate({ ...settings, enabled: v })}
              />
            </label>
          </div>
        </div>
        <ScheduleStatus canManage={canManage} settings={settings} onReview={onReview} />
        {canManage && open && (
          <div className="space-y-3 border-t border-border pt-3">
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              <FormField label="Auto-imports per scan" hint="1 to 1000; the rest wait for review">
                <Input
                  type="number"
                  min={1}
                  max={1000}
                  value={form.max_auto_import_per_run}
                  onChange={(e) => setForm({ ...form, max_auto_import_per_run: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
              <FormField label="License nodes kept free" hint="Auto-import stops before this reserve">
                <Input
                  type="number"
                  min={0}
                  value={form.license_reserve_nodes}
                  onChange={(e) => setForm({ ...form, license_reserve_nodes: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
              <FormField label="Keep rule history (days)" hint="7 to 3650">
                <Input
                  type="number"
                  min={7}
                  max={3650}
                  value={form.history_days}
                  onChange={(e) => setForm({ ...form, history_days: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
              <FormField
                label="Changes allowed per automatic run before approval"
                hint="A re-check (“whenever they change”) or scheduled run that would change more waits for review"
              >
                <Input
                  type="number"
                  min={1}
                  max={100000}
                  value={form.max_changes_per_run ?? 100}
                  onChange={(e) => setForm({ ...form, max_changes_per_run: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
              <FormField
                label="Growth stop (%)"
                hint="A rule that would change more assets than this share of what it matched last time waits for review; 0 turns it off"
              >
                <Input
                  type="number"
                  min={0}
                  max={10000}
                  value={form.growth_breaker_pct ?? 50}
                  onChange={(e) => setForm({ ...form, growth_breaker_pct: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
              <FormField label="Growth stop: at least (assets)" hint="Changes on fewer assets than this never trip the growth stop">
                <Input
                  type="number"
                  min={0}
                  max={100000}
                  value={form.growth_breaker_min ?? 20}
                  disabled={(form.growth_breaker_pct ?? 50) === 0}
                  onChange={(e) => setForm({ ...form, growth_breaker_min: Math.trunc(Number(e.target.value)) })}
                />
              </FormField>
            </div>
            <div className="space-y-2 rounded-md border border-border bg-surface2/30 p-3">
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!!form.schedule_enabled}
                  onChange={(e) => {
                    const enabled = e.target.checked
                    // Turning the schedule on for the first time: start from the browser's own time zone.
                    const fresh = enabled && !settings.schedule_enabled && (!form.schedule_timezone || form.schedule_timezone === 'UTC')
                    setForm({ ...form, schedule_enabled: enabled, ...(fresh ? { schedule_timezone: browserTimeZone() } : {}) })
                  }}
                />
                <span>
                  Run rules with the scheduled trigger on a schedule
                  <span className="block text-xs text-muted">
                    They run over every device and server they look at, with the same limits and approval as a re-check.
                    The first time after the schedule is turned on only marks the start.
                  </span>
                </span>
              </label>
              {form.schedule_enabled && (
                <div className="grid gap-3 md:grid-cols-3">
                  <FormField label="How often">
                    <Select
                      value={String(form.schedule_every_hours ?? 24)}
                      onValueChange={(v) => setForm({ ...form, schedule_every_hours: Number(v) })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {hours.map((h) => (
                          <SelectItem key={h} value={String(h)}>
                            {scheduleEveryText(h)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField
                    label={(form.schedule_every_hours ?? 24) < 24 ? 'Starting at' : 'At'}
                    hint={(form.schedule_every_hours ?? 24) < 24 ? 'Repeats from this time every day' : undefined}
                  >
                    <Input
                      type="time"
                      value={form.schedule_time ?? '02:00'}
                      onChange={(e) => setForm({ ...form, schedule_time: e.target.value })}
                    />
                  </FormField>
                  <FormField label="Time zone" hint="An IANA name, such as Asia/Riyadh or UTC">
                    <Input
                      list="discovery-rules-time-zones"
                      value={form.schedule_timezone ?? ''}
                      maxLength={64}
                      onChange={(e) => setForm({ ...form, schedule_timezone: e.target.value.trim() })}
                    />
                    <TimeZoneList id="discovery-rules-time-zones" />
                  </FormField>
                  <div className="text-xs text-muted md:col-span-3">{scheduleText(form)}</div>
                </div>
              )}
            </div>
            {phase4Problems.length > 0 && (
              <ul className="list-disc pl-5 text-xs text-warning-fg">
                {phase4Problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
            <div className="flex items-end gap-2">
              <Button
                onClick={() => save.mutate({ ...form, enabled: settings.enabled })}
                disabled={!dirty || !valid || save.isPending}
              >
                {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                Save settings
              </Button>
              {dirty && (
                <Button variant="ghost" onClick={() => setForm(settings)}>
                  Reset
                </Button>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** The browser's time zone names for the schedule's time zone field (none on a browser that cannot list them). */
function TimeZoneList({ id }: { id: string }) {
  const zones = useMemo(() => {
    try {
      const list = (Intl as any).supportedValuesOf?.('timeZone') as string[] | undefined
      return Array.isArray(list) ? Array.from(new Set(['UTC', ...list])) : ['UTC']
    } catch {
      return ['UTC']
    }
  }, [])
  return (
    <datalist id={id}>
      {zones.map((z) => (
        <option key={z} value={z} />
      ))}
    </datalist>
  )
}

/**
 * When the rules with the scheduled trigger run next (in the viewer's time), how many there are, the last scheduled run
 * with a link to it, and "Run scheduled rules now" for rule managers.
 */
function ScheduleStatus({
  canManage,
  settings,
  onReview,
}: {
  canManage: boolean
  settings: RuleEngineSettings
  onReview: (runId: string) => void
}) {
  const qc = useQueryClient()
  const [confirm, setConfirm] = useState(false)
  const { data: status } = useQuery<RuleScheduleStatus>({
    queryKey: SCHEDULE_KEY,
    queryFn: rulesApi.schedule,
    refetchInterval: 60_000,
    retry: false,
  })
  const runNow = useMutation({
    mutationFn: rulesApi.runScheduleNow,
    onSuccess: (r: ScheduleRunResult) => {
      setConfirm(false)
      qc.invalidateQueries({ queryKey: SCHEDULE_KEY })
      qc.invalidateQueries({ queryKey: HISTORY_KEY })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'conflicts'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'unmatched'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'asset'] })
      if (r.skipped) {
        toast.info('Scheduled rules did not run', scheduleRunText(r))
        if (r.skipped === 'waiting_for_approval' && r.engine_run_id) onReview(r.engine_run_id)
      } else if (r.needs_approval) {
        toast.warning('Scheduled run waits for approval', scheduleRunText(r))
        onReview(r.needs_approval)
      } else {
        toast.success('Scheduled rules ran', scheduleRunText(r))
      }
    },
    onError: (e: any) => {
      setConfirm(false)
      toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Could not run the scheduled rules', apiErrorMessage(e))
    },
  })
  if (!status) return null
  const total = status.rules + status.simulating
  if (!status.schedule_enabled && total === 0 && !status.last_run) return null
  const last = status.last_run
  const lastLink = last ? (last.status === 'needs_approval' ? `/discovery/rules?review=${last.id}` : `/discovery/rules?run=${last.id}`) : null
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-surface2/30 px-3 py-2 text-xs">
      <Clock className="h-3.5 w-3.5 shrink-0 text-primary" />
      <span className="font-medium">Scheduled rules</span>
      {status.schedule_enabled ? (
        <span className="text-muted">
          {scheduleText(settings)}
          {status.engine_enabled ? (
            <>
              {' '}· next{' '}
              <span className="text-text">{regionalDate(new Date(status.next), 'datetime')}</span> ({untilText(status.next)})
            </>
          ) : (
            ' · not running while discovery rules are off'
          )}
        </span>
      ) : (
        <span className="text-muted">The schedule is off (Settings)</span>
      )}
      <span className="text-muted">
        · {plural(status.rules, 'active rule')}
        {status.simulating ? `, ${status.simulating} simulating` : ''}
      </span>
      {last && lastLink && (
        <span className="text-muted">
          · last run{' '}
          <Link to={lastLink} className="text-primary hover:underline">
            {relativeTime(last.started_at)}
          </Link>{' '}
          <Badge variant={runStatusTone(last.status) as any} className="whitespace-nowrap">
            {runStatusLabel(last.status)}
          </Badge>
        </span>
      )}
      {canManage && (
        <Button
          variant="outline"
          size="sm"
          className="ml-auto h-7"
          disabled={!status.engine_enabled || total === 0 || runNow.isPending}
          title={!status.engine_enabled ? 'Discovery rules are off' : total === 0 ? 'No rule has the scheduled trigger' : undefined}
          onClick={() => setConfirm(true)}
        >
          {runNow.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          Run scheduled rules now
        </Button>
      )}
      <ConfirmDialog
        open={confirm}
        onOpenChange={(o) => !o && setConfirm(false)}
        title="Run the scheduled rules now?"
        description={
          <>
            {plural(status.rules, 'active rule')} with the scheduled trigger run over every device and server they look
            at{status.simulating ? `, and ${status.simulating} simulating rule${status.simulating === 1 ? '' : 's'} record what they would do` : ''}.
            The same limits apply as on a scheduled run: more than {settings.max_changes_per_run ?? 100} changes, a
            rule's own limit or the growth stop make the run wait for approval.
          </>
        }
        confirmText="Run now"
        loading={runNow.isPending}
        onConfirm={() => runNow.mutate()}
      />
    </div>
  )
}

/**
 * Devices, servers and controller-managed children that no active rule matches (optionally: counting simulating rules
 * as matching), whatever the rules' triggers: what the rules leave alone.
 */
function UnmatchedCard() {
  const [open, setOpen] = useState(false)
  const [kinds, setKinds] = useState<AssetKind[]>(['device', 'server', 'child'])
  const [withSimulating, setWithSimulating] = useState(false)
  const { data, isLoading, isFetching, error } = useQuery<UnmatchedReport>({
    queryKey: ['discovery', 'rules', 'unmatched', kinds.join(','), withSimulating],
    queryFn: () => rulesApi.unmatched(kinds, withSimulating),
    enabled: open && kinds.length > 0,
    staleTime: 30_000,
    retry: false,
  })
  const rows = data?.rows ?? []
  return (
    <Card>
      <CardHeader className="pb-3">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <CardTitle className="flex items-center gap-2 text-sm">
            <SearchX className="h-4 w-4 text-primary" /> Unmatched
          </CardTitle>
          {data && <Badge variant={data.unmatched ? 'warning' : 'success'}>{data.unmatched ? plural(data.unmatched, 'asset') : 'none'}</Badge>}
        </button>
        {open && (
          <p className="mt-1 text-xs text-muted">
            Devices and servers in inventory that no active rule matches, whatever the rules' triggers: what your rules
            leave alone.
          </p>
        )}
      </CardHeader>
      {open && (
        <CardContent className="space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {ASSET_KIND_ORDER.map((k) => (
              <label key={k} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={kinds.includes(k)}
                  onChange={(e) =>
                    setKinds(e.target.checked ? ASSET_KIND_ORDER.filter((x) => x === k || kinds.includes(x)) : kinds.filter((x) => x !== k))
                  }
                />
                {ASSET_KIND_SHORT[k]}
              </label>
            ))}
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={withSimulating} onChange={(e) => setWithSimulating(e.target.checked)} />
              Count simulating rules as matching
            </label>
            {isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
          </div>
          {kinds.length === 0 ? (
            <div className="py-4 text-center text-xs text-muted">Choose at least one kind of asset.</div>
          ) : isLoading ? (
            <div className="py-4 text-center text-muted">Loading…</div>
          ) : error ? (
            <div className="py-4 text-center text-danger-fg">{apiErrorMessage(error)}</div>
          ) : !data ? null : (
            <>
              <div className="text-xs text-muted">
                {data.rules === 0
                  ? `No ${withSimulating ? 'active or simulating' : 'active'} rule: nothing is matched. `
                  : `${plural(data.rules, withSimulating ? 'active or simulating rule' : 'active rule')} checked. `}
                Unmatched: {unmatchedKindsText(data) || 'none'}.
                {data.scoped ? ' The list shows only the assets your view includes; the counts cover all of them.' : ''}
                {data.truncated ? ` The first ${rows.length} are listed.` : ''}
              </div>
              {rows.length === 0 ? (
                <div className="py-4 text-center text-xs text-muted">Every asset is matched by a rule.</div>
              ) : (
                <div className="max-h-96 overflow-y-auto rounded-md border border-border">
                  <Table>
                    <THead className="bg-surface2/50">
                      <Tr>
                        <Th>Asset</Th>
                        <Th>Kind</Th>
                        <Th>Vendor / model / OS</Th>
                        <Th>Type</Th>
                        <Th>Tags</Th>
                      </Tr>
                    </THead>
                    <TBody>
                      {rows.map((r) => {
                        const href = assetPath(r.asset_kind, r.asset_id)
                        const what = [r.vendor, r.model].filter(Boolean).join(' ') || r.os || ''
                        return (
                          <Tr key={`${r.asset_kind}:${r.asset_id}`}>
                            <Td className="text-xs">
                              {href ? (
                                <Link to={href} className="font-medium text-primary hover:underline">
                                  {r.name || r.ip_address || r.asset_id.slice(0, 8)}
                                </Link>
                              ) : (
                                r.name || r.ip_address || '—'
                              )}
                              {r.ip_address && r.name && <div className="font-mono text-2xs text-muted">{r.ip_address}</div>}
                            </Td>
                            <Td className="whitespace-nowrap text-xs">{ASSET_KIND_SHORT[r.asset_kind] ?? r.asset_kind}</Td>
                            <Td className="text-xs">
                              {what || '—'}
                              {r.os && what !== r.os && <div className="text-2xs text-muted">{r.os}</div>}
                            </Td>
                            <Td className="text-xs">{r.device_type ? r.device_type.replace(/_/g, ' ') : '—'}</Td>
                            <Td className="text-xs">{r.tags?.length ? r.tags.join(', ') : '—'}</Td>
                          </Tr>
                        )
                      })}
                    </TBody>
                  </Table>
                </div>
              )}
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}

const OUTCOMES = Object.keys(OUTCOME_LABEL) as (keyof typeof OUTCOME_LABEL)[]

function ConflictsCard({ catalog }: { catalog: RuleCatalog | undefined }) {
  const [open, setOpen] = useState(false)
  const { data, isLoading, error } = useQuery<ConflictReport>({
    queryKey: ['discovery', 'rules', 'conflicts'],
    queryFn: rulesApi.conflicts,
    enabled: open,
    staleTime: 30_000,
  })
  const problems = data ? data.overridden.length + data.never_match.length + data.never_win.length : 0
  return (
    <Card>
      <CardHeader className="pb-3">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <CardTitle className="flex items-center gap-2 text-sm">
            <GitCompare className="h-4 w-4 text-primary" /> Conflicts
          </CardTitle>
          {data && <Badge variant={problems ? 'warning' : 'success'}>{problems ? plural(problems, 'finding') : 'none'}</Badge>}
        </button>
        {open && (
          <p className="mt-1 text-xs text-muted">
            The active rules evaluated against the devices and servers in inventory now: which rules override each
            other, and which never match or never win.
          </p>
        )}
      </CardHeader>
      {open && (
        <CardContent className="space-y-4 text-sm">
          {isLoading ? (
            <div className="py-4 text-center text-muted">Loading…</div>
          ) : error ? (
            <div className="py-4 text-center text-danger-fg">{apiErrorMessage(error)}</div>
          ) : !data || data.rules.length === 0 ? (
            <div className="py-4 text-center text-muted">No active rules.</div>
          ) : (
            <>
              <div className="text-xs text-muted">
                {plural(data.assets, 'device or server', 'devices and servers')} checked
                {data.truncated ? ' (the first ones only: the inventory is larger than one check covers)' : ''}.
              </div>
              <ConflictList title="Overridden by an earlier rule" empty="No rule's value is overridden by another rule.">
                {data.overridden.map((o, i) => (
                  <li key={i}>
                    <RuleLink id={o.rule_id} name={o.rule_name} />: {settingLabel(o.action_key, catalog).toLowerCase()} is
                    overridden by <RuleLink id={o.winner_rule_id} name={o.winner_rule_name} /> on{' '}
                    {plural(o.assets, 'asset')}
                  </li>
                ))}
              </ConflictList>
              <ConflictList title="Match nothing in inventory" empty="Every active rule matches something.">
                {data.never_match.map((r) => (
                  <li key={r.rule_id}>
                    <RuleLink id={r.rule_id} name={r.rule_name} />
                    <span className="text-muted"> — scan results may still match it</span>
                  </li>
                ))}
              </ConflictList>
              <ConflictList title="Match but never win" empty="Every matching rule sets at least one value somewhere.">
                {data.never_win.map((r) => (
                  <li key={r.rule_id}>
                    <RuleLink id={r.rule_id} name={r.rule_name} />
                    <span className="text-muted">
                      {' '}— matches {plural(r.matches, 'asset')}, every value it sets comes from an earlier rule or is
                      already set
                    </span>
                  </li>
                ))}
              </ConflictList>
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}

function ConflictList({ title, empty, children }: { title: string; empty: string; children: React.ReactNode[] }) {
  return (
    <div>
      <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">{title}</div>
      {children.length ? <ul className="list-disc space-y-0.5 pl-5 text-xs">{children}</ul> : <div className="text-xs text-muted">{empty}</div>}
    </div>
  )
}

function RuleLink({ id, name }: { id: string; name: string | null }) {
  return (
    <Link to={`/discovery/rules/${id}`} className="font-medium text-primary hover:underline">
      {name || id.slice(0, 8)}
    </Link>
  )
}

function HistoryCard({
  canManage,
  catalog,
  lookups,
  onReview,
}: {
  canManage: boolean
  catalog: RuleCatalog | undefined
  lookups: Lookups
  onReview: (runId: string) => void
}) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [params] = useSearchParams()
  const linked = params.get('run')
  const [expanded, setExpanded] = useState<string | null>(linked)
  const [undoing, setUndoing] = useState<EngineRun | null>(null)
  const [undone, setUndone] = useState<UndoResult | null>(null)
  const { data: runs = [], isLoading } = useRuleHistory()
  useEffect(() => {
    if (linked) setExpanded(linked)
  }, [linked])

  const undo = useMutation({
    mutationFn: (id: string) => rulesApi.undo(id),
    onSuccess: (r) => {
      setUndoing(null)
      setUndone(r)
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'history'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'conflicts'] })
      toast.success('Run undone', summarizeUndoResult(r))
    },
    onError: (e: any) => {
      setUndoing(null)
      toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Could not undo the run', apiErrorMessage(e))
    },
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">Recent rule runs</CardTitle>
        <p className="mt-1 text-xs text-muted">
          Each scan's evaluation, re-evaluations, runs on inventory, new devices and servers, automatic re-checks after
          changes, scheduled runs and undos. Open a run to see what every rule did.
        </p>
      </CardHeader>
      <CardContent className="p-0">
        {undone && <UndoResultPanel result={undone} onClose={() => setUndone(null)} />}
        {isLoading ? (
          <div className="py-8 text-center text-sm text-muted">Loading…</div>
        ) : runs.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted">Rules have not run yet.</div>
        ) : (
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th className="w-8"></Th>
                <Th>Started</Th>
                <Th>Scan</Th>
                <Th>Trigger</Th>
                <Th>Status</Th>
                <Th>Counts</Th>
                {canManage && <Th className="w-24"></Th>}
              </Tr>
            </THead>
            <TBody>
              {runs.map((r) => {
                const open = expanded === r.id
                return (
                  <Fragment key={r.id}>
                    <Tr
                      className={cn(
                        'cursor-pointer',
                        linked === r.id && 'bg-primary/5',
                        r.status === 'needs_approval' && 'bg-warning/10',
                      )}
                      onClick={() => setExpanded(open ? null : r.id)}
                    >
                      <Td>
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-label={open ? 'Hide details' : 'Show details'}
                          className="text-muted hover:text-text"
                        >
                          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                        </button>
                      </Td>
                      <Td className="whitespace-nowrap text-xs">
                        {regionalDate(new Date(r.started_at), 'datetime')}
                        <div className="text-2xs text-muted">{relativeTime(r.started_at)}</div>
                      </Td>
                      <Td className="text-xs">
                        {r.discovery_run_id ? (
                          <button
                            type="button"
                            className="text-primary hover:underline"
                            onClick={(e) => {
                              e.stopPropagation()
                              navigate(`/discovery/runs/${r.discovery_run_id}`)
                            }}
                          >
                            {r.profile_name || r.discovery_run_id.slice(0, 8)}
                          </button>
                        ) : (
                          '—'
                        )}
                      </Td>
                      <Td className="text-xs">
                        {runTriggerText(r)}
                        {(r.trigger === 'changed' || r.trigger === 'scheduled') && (
                          <div className="text-2xs text-muted">{TRIGGER_REASON[r.trigger]}</div>
                        )}
                      </Td>
                      <Td>
                        <Badge variant={runStatusTone(r.status) as any} className="whitespace-nowrap">
                          {runStatusLabel(r.status)}
                        </Badge>
                        {r.status === 'needs_approval' && (
                          <button
                            type="button"
                            className="ml-2 text-xs font-medium text-primary hover:underline"
                            onClick={(e) => {
                              e.stopPropagation()
                              onReview(r.id)
                            }}
                          >
                            {canManage ? 'Review' : 'View changes'}
                          </button>
                        )}
                        {r.detail && r.status !== 'needs_approval' && (r.status === 'rejected' || r.trigger === 'changed' || r.trigger === 'scheduled') && (
                          <div className="mt-0.5 max-w-xs text-2xs text-muted">{r.detail}</div>
                        )}
                        {r.approved_at && (
                          <div className="text-2xs text-muted">
                            {r.status === 'rejected' ? 'Decided' : 'Approved'} {relativeTime(r.approved_at)}
                          </div>
                        )}
                      </Td>
                      <Td className="text-xs text-muted">{runCountsText(r)}</Td>
                      {canManage && (
                        <Td className="text-right">
                          {canUndoRun(r) && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7"
                              title="Put back what this run changed"
                              onClick={(e) => {
                                e.stopPropagation()
                                setUndoing(r)
                              }}
                            >
                              <Undo2 className="h-3.5 w-3.5" /> Undo
                            </Button>
                          )}
                        </Td>
                      )}
                    </Tr>
                    {open && (
                      <Tr>
                        <Td colSpan={canManage ? 7 : 6} className="bg-surface2/30">
                          <HistoryItems runId={r.id} catalog={catalog} lookups={lookups} />
                        </Td>
                      </Tr>
                    )}
                  </Fragment>
                )
              })}
            </TBody>
          </Table>
        )}
      </CardContent>
      <ConfirmDialog
        open={!!undoing}
        onOpenChange={(o) => !o && setUndoing(null)}
        title="Undo this run?"
        description={
          undoing ? (
            undoing.discovery_run_id ? (
              <>
                A scan's run is not reverted value by value: the settings came with the import. You get the list of
                devices it imported, to delete from Devices if they should not be monitored. Undo never deletes devices.
              </>
            ) : (
              <>
                Every value this run set that is still as it left it is put back
                {undoing.counts?.applied != null ? ` (${plural(Number(undoing.counts.applied), 'value')} applied)` : ''}.
                Values changed since are listed and left alone. A run can be undone once.
              </>
            )
          ) : undefined
        }
        confirmText="Undo run"
        loading={undo.isPending}
        onConfirm={() => {
          if (undoing) undo.mutate(undoing.id)
        }}
      />
    </Card>
  )
}

function runCountsText(r: EngineRun): string {
  const c = r.counts || {}
  if (r.trigger === 'undo') return summarizeUndoResult({ ...c, imported_devices: undefined })
  if (r.status === 'needs_approval' || (r.status === 'rejected' && c.applied == null)) {
    return `${plural(pendingCount(r), 'change')} on ${plural(Number(c.assets_changed ?? 0), 'asset')} ${r.status === 'rejected' ? 'not applied' : 'held'}`
  }
  if ((r.trigger === 'manual' && !r.discovery_run_id) || r.trigger === 'changed' || r.trigger === 'scheduled') {
    const parts = [`matched ${Number(c.matched ?? 0)} of ${Number(c.assets ?? 0)}`, `applied ${Number(c.applied ?? 0)}`]
    if (c.reverted) parts.push(`reverted ${c.reverted}`)
    if (c.kept_manual_edit) parts.push(`hand edits kept ${c.kept_manual_edit}`)
    if (c.skipped_changed) parts.push(`skipped ${c.skipped_changed}`)
    if (c.not_ready) parts.push(`waiting for a sensor ${c.not_ready}`)
    return sentenceCase(parts.join(' · '))
  }
  return summarizeCounts(c)
}

function UndoResultPanel({ result, onClose }: { result: UndoResult; onClose: () => void }) {
  return (
    <div className="m-3 space-y-2 rounded-md border border-border bg-surface2/40 p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium">Undo finished: {result.status}</div>
          <div className="text-xs text-muted">{summarizeUndoResult(result)}</div>
        </div>
        <Button variant="ghost" size="sm" className="h-7" onClick={onClose}>
          Close
        </Button>
      </div>
      {result.imported_devices.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs">
            The scan imported these devices. Delete them from Devices if they should not be monitored; undo never
            deletes devices.
          </div>
          <ul className="max-h-48 space-y-0.5 overflow-y-auto text-xs">
            {result.imported_devices.map((d) => (
              <li key={d.device_id}>
                <Link to={`/devices/${d.device_id}`} className="text-primary hover:underline">
                  {d.hostname || d.ip_address || d.device_id.slice(0, 8)}
                </Link>
                {d.ip_address && d.hostname && <span className="ml-1 font-mono text-muted">{d.ip_address}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function HistoryItems({ runId, catalog, lookups: baseLookups }: { runId: string; catalog: RuleCatalog | undefined; lookups: Lookups }) {
  const [outcome, setOutcome] = useState<string>('all')
  const { data: items = [], isLoading, error } = useQuery({
    queryKey: ['discovery', 'rules', 'history', runId, 'items', outcome],
    queryFn: () => rulesApi.historyItems(runId, { outcome: outcome === 'all' ? undefined : outcome, limit: 500 }),
  })
  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(items), [items]))
  return (
    <div className="space-y-2 py-1">
      <div className="flex flex-wrap items-center gap-1">
        {['all', ...OUTCOMES].map((o) => (
          <button
            key={o}
            type="button"
            onClick={() => setOutcome(o)}
            className={cn(
              'whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium',
              outcome === o ? 'bg-primary/10 text-text' : 'text-muted hover:bg-surface2 hover:text-text',
            )}
          >
            {o === 'all' ? 'All' : OUTCOME_LABEL[o as keyof typeof OUTCOME_LABEL]}
          </button>
        ))}
      </div>
      {isLoading ? (
        <div className="py-4 text-center text-xs text-muted">Loading…</div>
      ) : error ? (
        <div className="py-4 text-center text-xs text-danger">{apiErrorMessage(error)}</div>
      ) : items.length === 0 ? (
        <div className="py-4 text-center text-xs text-muted">No items{outcome !== 'all' ? ' with this outcome' : ''}.</div>
      ) : (
        <div className="max-h-96 overflow-y-auto rounded-md border border-border bg-surface">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Asset</Th>
                <Th>Rule</Th>
                <Th>Setting</Th>
                <Th>Before → after</Th>
                <Th>Outcome</Th>
                <Th>Detail</Th>
              </Tr>
            </THead>
            <TBody>
              {items.map((it) => {
                const href = assetPath(it.asset_kind, it.asset_id)
                return (
                  <Tr key={it.id}>
                    <Td className="text-xs">
                      {href ? (
                        <Link to={href} className="font-mono text-primary hover:underline">
                          {it.ip_address || it.asset_id?.slice(0, 8)}
                        </Link>
                      ) : (
                        <span className="font-mono">{it.ip_address || '—'}</span>
                      )}
                      {it.asset_kind === 'server' && <div className="text-2xs text-muted">server</div>}
                      {it.asset_kind === 'child' && <div className="text-2xs text-muted">managed switch / AP</div>}
                    </Td>
                    <Td className="text-xs">
                      {it.rule_id ? <RuleLink id={it.rule_id} name={it.rule_name} /> : it.rule_name || '—'}
                    </Td>
                    <Td className="text-xs">{it.action_key ? settingLabel(it.action_key, catalog) : '—'}</Td>
                    <Td className="text-xs">
                      {it.action_key && it.action_key !== 'decision' && (it.before !== undefined || it.after != null)
                        ? changeText(it.action_key, it.before ?? null, it.after ?? null, lookups, it.item_key)
                        : it.after != null
                          ? formatAfter(it.after)
                          : '—'}
                    </Td>
                    <Td>
                      <Badge variant={(OUTCOME_TONE as Record<string, string>)[it.outcome] as any ?? 'default'} className="whitespace-nowrap">
                        {itemOutcomeLabel(it.outcome, it.action_key)}
                      </Badge>
                    </Td>
                    <Td className="text-xs text-muted">{it.detail || '—'}</Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function formatAfter(v: any): string {
  if (v == null) return ''
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') {
    if ('value' in v) return formatAfter(v.value)
    return Object.entries(v).map(([k, x]) => `${k}: ${Array.isArray(x) ? x.join(', ') : String(x)}`).join(', ')
  }
  return String(v)
}

// ── phase 3: approval, export / import, starter rules ───────────────────────

const MAX_REVIEW_ROWS = 500

/**
 * The changes an automatic re-check holds for approval (?review=<run id>): what each would do, then Approve (each value
 * is re-checked before it is written) or Reject (not proposed again until an asset or a rule changes). Read-only
 * viewers see the list without the buttons.
 */
function ReviewPanel({
  runId,
  run,
  canManage,
  catalog,
  lookups: baseLookups,
  onClose,
}: {
  runId: string
  run: EngineRun | undefined
  canManage: boolean
  catalog: RuleCatalog | undefined
  lookups: Lookups
  onClose: () => void
}) {
  const qc = useQueryClient()
  const ref = useRef<HTMLDivElement>(null)
  const [confirm, setConfirm] = useState<'approve' | 'reject' | null>(null)
  const [result, setResult] = useState<ApprovalResult | null>(null)
  const [refused, setRefused] = useState<string | null>(null)
  const [show, setShow] = useState<'all' | (typeof CHANGE_OUTCOME_ORDER)[number]>('all')
  const { data, isLoading, error } = useQuery<PendingChanges>({
    queryKey: ['discovery', 'rules', 'pending', runId],
    queryFn: () => rulesApi.pending(runId),
    retry: false,
  })
  const changes = data?.changes ?? []
  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(changes), [changes]))
  useEffect(() => {
    ref.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [])

  const refresh = () => {
    qc.invalidateQueries({ queryKey: HISTORY_KEY })
    qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'pending', runId] })
    qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'conflicts'] })
    qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'asset'] })
  }
  const decide = useMutation({
    mutationFn: (what: 'approve' | 'reject') => (what === 'approve' ? rulesApi.approve(runId) : rulesApi.reject(runId)),
    onSuccess: (r, what) => {
      setConfirm(null)
      setResult(r)
      setRefused(null)
      refresh()
      if (what === 'approve') toast.success('Changes approved', summarizeApprovalResult(r))
      else toast.success('Changes rejected', 'They are not proposed again until an asset or a rule changes.')
    },
    onError: (e: any) => {
      setConfirm(null)
      const status = e?.response?.status
      if (status === 409) {
        // The rules changed since the plan (the run is set aside and planned again) or it no longer waits.
        setRefused(apiErrorMessage(e))
        refresh()
        return
      }
      toast.error(status === 403 ? 'Not allowed' : 'Could not record the decision', apiErrorMessage(e))
    },
  })

  const waiting = data?.status === 'needs_approval' && !result
  const total = pendingCount({ counts: data?.counts })
  const rows = changes.filter((c) => show === 'all' || c.outcome === show)
  const shown = rows.slice(0, MAX_REVIEW_ROWS)
  const hidden = total > changes.length ? total - changes.filter((c) => c.outcome === 'apply' || c.outcome === 'revert').length : 0

  return (
    <Card ref={ref} className={cn('scroll-mt-20', waiting ? 'border-warning/50' : 'border-border')}>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3 pb-3">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2 text-base">
            <ClipboardCheck className="h-4 w-4 text-primary" />
            {waiting ? 'Changes waiting for approval' : (data?.trigger ?? run?.trigger) === 'scheduled' ? 'Scheduled run' : 'Automatic re-check'}
            {data && (
              <Badge variant={runStatusTone(result?.status ?? data.status) as any}>{runStatusLabel(result?.status ?? data.status)}</Badge>
            )}
          </CardTitle>
          <p className="mt-1 text-xs text-muted">
            {(data?.trigger ?? run?.trigger) === 'scheduled' ? 'Scheduled run' : (data?.trigger ?? run?.trigger) === 'changed' ? 'Re-check after changes' : 'Rule run'}
            {run ? ` · planned ${relativeTime(run.started_at)}` : ''}
            {data?.detail && !data.reasons?.length ? ` · ${data.detail}` : ''}
          </p>
          {data && (
            <p className="mt-1 text-sm">{sentenceCase(summarizeInventoryPreview(data.counts || {}))}.</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canManage && waiting && (
            <>
              <Button variant="outline" onClick={() => setConfirm('reject')} disabled={decide.isPending}>
                <X className="h-4 w-4" /> Reject
              </Button>
              <Button onClick={() => setConfirm('approve')} disabled={decide.isPending || total === 0}>
                <Check className="h-4 w-4" /> Approve {total ? plural(total, 'change') : ''}
              </Button>
            </>
          )}
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {(data?.reasons?.length ?? 0) > 0 && (
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-fg">
            <div className="mb-0.5 font-semibold">{waiting ? 'Why it waits' : 'Why it waited'}</div>
            <ul className="list-disc space-y-0.5 pl-4">
              {data!.reasons!.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}
        {!canManage && waiting && (
          <div className="text-xs text-muted">
            Approving or rejecting needs the Discovery “Manage rules” permission (and device or server management for
            the assets involved).
          </div>
        )}
        {result && (
          <div className="flex items-start gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm text-success-fg">
            <Check className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              {summarizeApprovalResult(result)}
              {!!result.skipped_changed && (
                <div className="text-xs">Values skipped had changed since the plan; they are left as they are.</div>
              )}
            </div>
          </div>
        )}
        {refused && (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning-fg">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              {refused}
              <div className="text-xs">Nothing was applied. The next automatic re-check plans the changes again.</div>
            </div>
          </div>
        )}
        {isLoading ? (
          <div className="py-6 text-center text-sm text-muted">Loading…</div>
        ) : error ? (
          <div className="py-6 text-center text-sm text-danger-fg">{apiErrorMessage(error, 'Could not load the changes')}</div>
        ) : changes.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted">No changes you can see in this run.</div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-1">
              {(['all', ...CHANGE_OUTCOME_ORDER] as const).map((k) => {
                const n = k === 'all' ? changes.length : changes.filter((c) => c.outcome === k).length
                if (k !== 'all' && n === 0) return null
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setShow(k)}
                    className={cn(
                      'whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium',
                      show === k ? 'bg-primary/10 text-text' : 'text-muted hover:bg-surface2 hover:text-text',
                    )}
                  >
                    {k === 'all' ? 'All' : CHANGE_OUTCOME_LABEL[k]} ({n})
                  </button>
                )
              })}
              {rows.length > shown.length && <span className="ml-auto text-xs text-muted">first {shown.length} shown</span>}
            </div>
            {hidden > 0 && waiting && (
              <div className="text-xs text-muted">
                {plural(hidden, 'change')} on assets outside your view are not listed; an approval applies them too.
              </div>
            )}
            <ChangesTable changes={shown} catalog={catalog} lookups={lookups} />
          </>
        )}
      </CardContent>
      <ConfirmDialog
        open={confirm === 'approve'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title="Approve these changes?"
        description={
          <>
            {plural(total, 'change')} {total === 1 ? 'is' : 'are'} applied now. Each value is checked again first: one
            changed since the plan is skipped and listed. If the rules changed since, nothing is applied and the
            changes are planned again. The run can be undone from the history.
          </>
        }
        confirmText="Approve"
        loading={decide.isPending}
        onConfirm={() => decide.mutate('approve')}
      />
      <ConfirmDialog
        open={confirm === 'reject'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title="Reject these changes?"
        description="Nothing is changed. They are not proposed again until an asset or a rule changes."
        confirmText="Reject"
        destructive
        loading={decide.isPending}
        onConfirm={() => decide.mutate('reject')}
      />
    </Card>
  )
}

/** Download every rule as a JSON file (references by name; no ids, no secrets). */
function ExportButton({ disabled }: { disabled?: boolean }) {
  const exp = useMutation({
    mutationFn: rulesApi.exportRules,
    onSuccess: (doc) => {
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = exportFileName(new Date())
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success('Rules exported', plural(doc.rules?.length ?? 0, 'rule'))
    },
    onError: (e: any) => toast.error('Could not export the rules', apiErrorMessage(e)),
  })
  return (
    <Button size="sm" variant="outline" onClick={() => exp.mutate()} disabled={disabled || exp.isPending} title="Download every rule as a JSON file">
      {exp.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />} Export
    </Button>
  )
}

/** Read a local export file and create its rules as Drafts, last in the order; then list what was created or skipped. */
function ImportButton() {
  const qc = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [result, setResult] = useState<(RulesImportResult & { file: string }) | null>(null)
  const imp = useMutation({
    mutationFn: ({ document }: { document: Record<string, any>; file: string }) => rulesApi.importRules(document),
    onSuccess: (r, v) => {
      setResult({ ...r, file: v.file })
      qc.invalidateQueries({ queryKey: RULES_KEY })
    },
    onError: (e: any) => toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Could not import the rules', apiErrorMessage(e)),
  })
  const onFile = (file: File | undefined) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const parsed = parseRulesFile(String(reader.result ?? ''))
      if ('error' in parsed) {
        toast.error('Could not import the rules', parsed.error)
        return
      }
      imp.mutate({ document: parsed.document, file: file.name })
    }
    reader.onerror = () => toast.error('Could not read the file', file.name)
    reader.readAsText(file)
  }
  return (
    <>
      <input
        ref={input}
        type="file"
        accept=".json,application/json"
        className="hidden"
        aria-label="Rules file to import"
        onChange={(e) => {
          onFile(e.target.files?.[0])
          e.target.value = ''
        }}
      />
      <Button size="sm" variant="outline" onClick={() => input.current?.click()} disabled={imp.isPending} title="Create rules from an exported file, as drafts">
        {imp.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />} Import
      </Button>
      <Dialog open={!!result} onOpenChange={(o) => !o && setResult(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Import finished</DialogTitle>
          </DialogHeader>
          {result && (
            <div className="space-y-3 text-sm">
              <p className="text-xs text-muted">
                {result.file}: {plural(result.created.length, 'rule')} created
                {result.skipped.length ? `, ${result.skipped.length} skipped` : ''}. Imported rules are Drafts, placed
                last: review and preview each one before you make it active.
              </p>
              {result.created.length > 0 && (
                <div>
                  <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Created as drafts</div>
                  <ul className="max-h-56 space-y-1 overflow-y-auto">
                    {result.created.map((c) => (
                      <li key={c.id}>
                        <Link to={`/discovery/rules/${c.id}`} className="font-medium text-primary hover:underline" onClick={() => setResult(null)}>
                          {c.name}
                        </Link>
                        {c.notes?.length > 0 && (
                          <ul className="ml-4 list-disc text-xs text-muted">
                            {c.notes.map((n, i) => (
                              <li key={i}>{n}</li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {result.skipped.length > 0 && (
                <div>
                  <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Skipped</div>
                  <ul className="max-h-48 space-y-1 overflow-y-auto text-xs">
                    {result.skipped.map((k, i) => (
                      <li key={i}>
                        <span className="font-medium">{k.name || 'Unnamed rule'}</span>
                        <span className="text-warning-fg"> — {k.reason}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button onClick={() => setResult(null)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Starter rules (vendor tags by sysObjectID subtree, printer / IP phone ignore examples), added as Drafts. */
function StarterDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [picked, setPicked] = useState<string[]>([])
  const { data: starters = [], isLoading, error } = useQuery<StarterRule[]>({
    queryKey: ['discovery', 'rules', 'starter'],
    queryFn: rulesApi.starter,
    enabled: open,
  })
  useEffect(() => {
    if (open) setPicked([])
  }, [open])
  const add = useMutation({
    mutationFn: (keys: string[]) => rulesApi.addStarter(keys),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: RULES_KEY })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'starter'] })
      onOpenChange(false)
      toast.success(
        r.created.length ? `${plural(r.created.length, 'starter rule')} added as drafts` : 'Nothing added',
        r.created.length ? 'Review and preview them, then make them active.' : 'Those starter rules were already added.',
      )
    },
    onError: (e: any) => toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Could not add the starter rules', apiErrorMessage(e)),
  })
  const addable = starters.filter((s) => !s.added)
  return (
    <Dialog open={open} onOpenChange={(o) => !add.isPending && onOpenChange(o)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Add starter rules</DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted">
          Ready-made rules to start from. They arrive as <span className="font-medium text-text">Drafts</span>, last in
          the order: nothing runs until you review, preview and activate them.
        </p>
        {isLoading ? (
          <div className="py-6 text-center text-sm text-muted">Loading…</div>
        ) : error ? (
          <div className="py-6 text-center text-sm text-danger-fg">{apiErrorMessage(error)}</div>
        ) : (
          <ul className="max-h-80 space-y-1 overflow-y-auto">
            {starters.map((s) => (
              <li key={s.key}>
                <label className={cn('flex items-start gap-2 rounded-md px-2 py-1.5 text-sm', s.added ? 'opacity-70' : 'hover:bg-surface2')}>
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    disabled={s.added}
                    checked={s.added || picked.includes(s.key)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, s.key] : picked.filter((k) => k !== s.key))}
                  />
                  <span className="min-w-0">
                    <span className="font-medium">{s.name}</span>
                    {s.added && (
                      <Badge variant="outline" className="ml-2">
                        Added
                      </Badge>
                    )}
                    <span className="block text-xs text-muted">{s.description}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <DialogFooter>
          {addable.length > 0 && (
            <Button
              variant="ghost"
              className="mr-auto"
              onClick={() => setPicked(picked.length === addable.length ? [] : addable.map((s) => s.key))}
            >
              {picked.length === addable.length ? 'Clear' : 'Select all'}
            </Button>
          )}
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={add.isPending}>
            Cancel
          </Button>
          <Button onClick={() => add.mutate(picked)} disabled={!picked.length || add.isPending}>
            {add.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Add {picked.length ? plural(picked.length, 'rule') : ''} as drafts
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
