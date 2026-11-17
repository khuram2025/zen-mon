import { Fragment, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  Copy,
  ListChecks,
  Loader2,
  Pencil,
  Plus,
  Power,
  Settings2,
  Trash2,
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { rulesApi } from './api'
import type { DiscoveryRule, EngineRun, RuleEngineSettings, RuleMode } from './types'
import {
  MODE_LABEL,
  OUTCOME_LABEL,
  OUTCOME_TONE,
  moveItem,
  summarizeActions,
  sentenceCase,
  summarizeCondition,
  summarizeCounts,
} from './ruleModel'
import { ModeBadge, useRuleCatalog, useRuleLookups } from './ruleUi'

const RULES_KEY = ['discovery', 'rules', 'list'] as const
const SETTINGS_KEY = ['discovery', 'rules', 'settings'] as const

export function RulesPage() {
  const can = useCan()
  const canManage = can('discovery.manage_rules')
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data: catalog } = useRuleCatalog()
  const { lookups } = useRuleLookups()
  const [deleting, setDeleting] = useState<DiscoveryRule | null>(null)

  const { data: rules = [], isLoading } = useQuery<DiscoveryRule[]>({
    queryKey: RULES_KEY,
    queryFn: rulesApi.list,
  })

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
      <EngineCard canManage={canManage} activeCount={rules.filter((r) => r.mode === 'active').length} />

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm">
              <ListChecks className="h-4 w-4 text-primary" />
              Rules
            </CardTitle>
            <p className="mt-1 text-xs text-muted">
              Evaluated top to bottom on every scan result. The first matching rule sets each setting; tags from every
              matching rule are merged.
            </p>
          </div>
          {canManage && (
            <Button size="sm" onClick={() => navigate('/discovery/rules/new')}>
              <Plus className="h-4 w-4" /> New rule
            </Button>
          )}
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
                <Button onClick={() => navigate('/discovery/rules/new')}>
                  <Plus className="h-4 w-4" /> New rule
                </Button>
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
                            <SelectItem value="draft">Draft</SelectItem>
                            <SelectItem value="active">Active</SelectItem>
                            <SelectItem value="disabled">Disabled</SelectItem>
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
                        {r.asset_kinds.map((k) => (k === 'device' ? 'Network devices' : 'Servers')).join(' + ')}
                        {r.profile_ids?.length
                          ? ` · ${r.profile_ids.map((p) => lookups.discovery_profiles?.[p] ?? 'profile').join(', ')} only`
                          : ''}
                        {r.stop_processing ? ' · stops further rules' : ''}
                      </div>
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

      <HistoryCard />

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


function EngineCard({ canManage, activeCount }: { canManage: boolean; activeCount: number }) {
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
  const dirty =
    form.max_auto_import_per_run !== settings.max_auto_import_per_run ||
    form.license_reserve_nodes !== settings.license_reserve_nodes ||
    form.history_days !== settings.history_days
  const valid =
    Number.isInteger(form.max_auto_import_per_run) && form.max_auto_import_per_run >= 1 && form.max_auto_import_per_run <= 1000 &&
    Number.isInteger(form.license_reserve_nodes) && form.license_reserve_nodes >= 0 &&
    Number.isInteger(form.history_days) && form.history_days >= 7 && form.history_days <= 3650

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
        {canManage && open && (
          <div className="grid gap-3 border-t border-border pt-3 md:grid-cols-4">
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

const OUTCOMES = Object.keys(OUTCOME_LABEL) as (keyof typeof OUTCOME_LABEL)[]

function HistoryCard() {
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState<string | null>(null)
  const { data: runs = [], isLoading } = useQuery<EngineRun[]>({
    queryKey: ['discovery', 'rules', 'history'],
    queryFn: () => rulesApi.history({ limit: 25 }),
    refetchInterval: 30_000,
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">Recent rule runs</CardTitle>
        <p className="mt-1 text-xs text-muted">
          Each scan's evaluation, and each manual re-evaluation. Open a run to see what every rule did to each result.
        </p>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="py-8 text-center text-sm text-muted">Loading…</div>
        ) : runs.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted">Rules have not run on a scan yet.</div>
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
              </Tr>
            </THead>
            <TBody>
              {runs.map((r) => {
                const open = expanded === r.id
                return (
                  <Fragment key={r.id}>
                    <Tr className="cursor-pointer" onClick={() => setExpanded(open ? null : r.id)}>
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
                        {r.trigger === 'manual' ? 'Re-evaluated' : 'Scan'} ·{' '}
                        {r.mode === 'apply' ? 'apply' : 'annotate only'}
                      </Td>
                      <Td>
                        <Badge
                          variant={
                            r.status === 'completed' ? 'success' : r.status === 'partial' ? 'warning' : r.status === 'failed' ? 'danger' : 'outline'
                          }
                        >
                          {r.status}
                        </Badge>
                      </Td>
                      <Td className="text-xs text-muted">{summarizeCounts(r.counts || {})}</Td>
                    </Tr>
                    {open && (
                      <Tr>
                        <Td colSpan={6} className="bg-surface2/30">
                          <HistoryItems runId={r.id} />
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
    </Card>
  )
}

function HistoryItems({ runId }: { runId: string }) {
  const [outcome, setOutcome] = useState<string>('all')
  const { data: items = [], isLoading, error } = useQuery({
    queryKey: ['discovery', 'rules', 'history', runId, 'items', outcome],
    queryFn: () => rulesApi.historyItems(runId, { outcome: outcome === 'all' ? undefined : outcome, limit: 500 }),
  })
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
                <Th>IP</Th>
                <Th>Rule</Th>
                <Th>Action</Th>
                <Th>Outcome</Th>
                <Th>Detail</Th>
              </Tr>
            </THead>
            <TBody>
              {items.map((it) => (
                <Tr key={it.id}>
                  <Td className="font-mono text-xs">{it.ip_address || '—'}</Td>
                  <Td className="text-xs">{it.rule_name || '—'}</Td>
                  <Td className="text-xs">
                    {it.action_key || '—'}
                    {it.after != null && it.action_key && (
                      <span className="text-muted"> → {formatAfter(it.after)}</span>
                    )}
                  </Td>
                  <Td>
                    <Badge variant={(OUTCOME_TONE as Record<string, string>)[it.outcome] as any ?? 'default'}>
                      {(OUTCOME_LABEL as Record<string, string>)[it.outcome] ?? it.outcome}
                    </Badge>
                  </Td>
                  <Td className="text-xs text-muted">
                    {it.detail || '—'}
                    {it.asset_id && (it.asset_kind === 'device' || it.asset_kind === 'server') && (
                      <Link
                        to={`/${it.asset_kind === 'server' ? 'servers' : 'devices'}/${it.asset_id}`}
                        className="ml-1 text-primary hover:underline"
                      >
                        open
                      </Link>
                    )}
                  </Td>
                </Tr>
              ))}
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
