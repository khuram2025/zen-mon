/** Software Baselines — declare required / prohibited software per server class.
 *  The backend evaluates servers against each baseline's rules and raises
 *  alerts on violations; this page manages baselines and shows compliance. */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ClipboardCheck, ListChecks, Pencil, Plus, RefreshCw, Server, ShieldAlert, Trash2, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { KpiTile, TagList } from '@/components/servers/shared'
import type {
  Baseline, BaselineMatchType, BaselineRule, BaselineRuleInput, BaselineRuleType,
  ComplianceResult, ComplianceStatus, Severity,
} from '@/types/servers'

const OS_SCOPE_LABELS: Record<string, string> = {
  windows: 'Windows',
  linux: 'Linux',
  macos: 'macOS',
}

const RESULT_STATUS_META: Record<ComplianceStatus, { label: string; variant: 'success' | 'warning' | 'danger' }> = {
  compliant: { label: 'Compliant', variant: 'success' },
  missing: { label: 'Missing', variant: 'danger' },
  outdated: { label: 'Outdated', variant: 'warning' },
  prohibited: { label: 'Prohibited', variant: 'danger' },
}

export function BaselinesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Baseline | null>(null)
  const [deleting, setDeleting] = useState<Baseline | null>(null)
  const [resultsFor, setResultsFor] = useState<Baseline | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['server-baselines'],
    queryFn: async () => (await api.get('/server-baselines')).data as { items: Baseline[] },
    refetchInterval: 30_000,
  })
  const items = data?.items || []

  const totalRules = items.reduce((s, b) => s + b.rule_count, 0)
  const totalEvaluated = items.reduce((s, b) => s + b.servers_evaluated, 0)
  const totalViolations = items.reduce((s, b) => s + b.violations, 0)

  const toggleEnabled = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) =>
      (await api.patch(`/server-baselines/${id}`, { enabled })).data,
    onSuccess: (_d, vars) => {
      toast.success(vars.enabled ? 'Baseline enabled' : 'Baseline disabled')
      qc.invalidateQueries({ queryKey: ['server-baselines'] })
    },
    onError: (e) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const evaluate = useMutation({
    mutationFn: async (b: Baseline) =>
      (await api.post(`/server-baselines/${b.id}/evaluate`)).data as { ok: boolean; servers_evaluated: number },
    onSuccess: (d) => {
      toast.success('Evaluation complete', `${d.servers_evaluated} server${d.servers_evaluated === 1 ? '' : 's'} evaluated`)
      qc.invalidateQueries({ queryKey: ['server-baselines'] })
    },
    onError: (e) => toast.error('Evaluation failed', apiErrorMessage(e)),
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/server-baselines/${id}`),
    onSuccess: () => {
      toast.success('Baseline deleted')
      qc.invalidateQueries({ queryKey: ['server-baselines'] })
      setDeleting(null)
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const openCreate = () => { setEditing(null); setFormOpen(true) }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <ClipboardCheck className="h-5 w-5 text-primary" />
            Software Baselines
          </h1>
          <p className="text-xs text-muted">
            Declare required or prohibited software per server class — violations raise alerts automatically
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="h-4 w-4" />
          New baseline
        </Button>
      </div>

      {isLoading ? (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[66px]" />)}
          </div>
          <Card>
            <CardContent className="space-y-2 pt-4">
              {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
            </CardContent>
          </Card>
        </>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <KpiTile icon={ClipboardCheck} label="Baselines" value={items.length} />
            <KpiTile icon={ListChecks} label="Rules" value={totalRules} />
            <KpiTile icon={Server} label="Evaluations" value={totalEvaluated} sub="servers in scope" />
            <KpiTile
              icon={ShieldAlert}
              label="Open violations"
              value={totalViolations}
              tone={totalViolations > 0 ? 'danger' : 'success'}
            />
          </div>

          <Card>
            <CardContent className="pt-4">
              <div className="overflow-hidden rounded-md border border-border">
                <Table>
                  <THead className="bg-surface2/50">
                    <Tr>
                      <Th>Name</Th>
                      <Th>Scope</Th>
                      <Th>Rules</Th>
                      <Th>Compliance</Th>
                      <Th>Violations</Th>
                      <Th>Alerting</Th>
                      <Th>Enabled</Th>
                      <Th>Updated</Th>
                      <Th className="w-32 text-right">Actions</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {items.map((b) => (
                      <Tr key={b.id}>
                        <Td>
                          <div className="font-medium">{b.name}</div>
                          {b.description && <div className="text-xs text-muted">{b.description}</div>}
                        </Td>
                        <Td>
                          <div className="space-y-1">
                            <Badge variant="outline">
                              {b.os_type ? OS_SCOPE_LABELS[b.os_type] || b.os_type : 'Any OS'}
                            </Badge>
                            {b.match_tags.length > 0 && <TagList tags={b.match_tags} />}
                          </div>
                        </Td>
                        <Td className="tabular-nums">{b.rule_count}</Td>
                        <Td>
                          {b.servers_evaluated === 0 ? (
                            <span className="text-xs text-muted">not evaluated</span>
                          ) : (
                            <div className="space-y-1">
                              <div className="text-xs tabular-nums text-text2">
                                {b.servers_compliant}/{b.servers_evaluated} servers
                              </div>
                              <div className="h-1.5 w-20 overflow-hidden rounded-full bg-surface2">
                                <div
                                  className={cn(
                                    'h-full rounded-full transition-all',
                                    b.servers_compliant >= b.servers_evaluated ? 'bg-success' : 'bg-warning',
                                  )}
                                  style={{ width: `${Math.round((b.servers_compliant / b.servers_evaluated) * 100)}%` }}
                                />
                              </div>
                            </div>
                          )}
                        </Td>
                        <Td>
                          {b.violations > 0
                            ? <Badge variant="danger">{b.violations}</Badge>
                            : <Badge variant="success">0</Badge>}
                        </Td>
                        <Td>
                          <Badge variant={b.alerting ? 'success' : 'outline'}>{b.alerting ? 'On' : 'Off'}</Badge>
                        </Td>
                        <Td>
                          <Switch
                            checked={b.enabled}
                            onCheckedChange={(v) => toggleEnabled.mutate({ id: b.id, enabled: v })}
                          />
                        </Td>
                        <Td className="text-xs text-muted">{relativeTime(b.updated_at)}</Td>
                        <Td>
                          <div className="flex justify-end gap-0.5">
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7"
                              onClick={() => setResultsFor(b)}
                              title="Results"
                            >
                              <ListChecks className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7"
                              onClick={() => evaluate.mutate(b)}
                              disabled={evaluate.isPending}
                              title="Evaluate now"
                            >
                              <RefreshCw className={cn(
                                'h-3.5 w-3.5',
                                evaluate.isPending && evaluate.variables?.id === b.id && 'animate-spin',
                              )} />
                            </Button>
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7"
                              onClick={() => { setEditing(b); setFormOpen(true) }}
                              title="Edit"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                              onClick={() => setDeleting(b)}
                              title="Delete"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </Td>
                      </Tr>
                    ))}
                    {items.length === 0 && (
                      <Tr>
                        <Td colSpan={9} className="py-12 text-center">
                          <div className="flex flex-col items-center gap-3">
                            <ClipboardCheck className="h-8 w-8 text-muted/50" />
                            <div className="text-sm text-muted">
                              No baselines yet — declare your first software baseline
                            </div>
                            <Button size="sm" onClick={openCreate}>
                              <Plus className="h-4 w-4" />
                              New baseline
                            </Button>
                          </div>
                        </Td>
                      </Tr>
                    )}
                  </TBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        </>
      )}

      <BaselineFormDialog open={formOpen} onOpenChange={setFormOpen} baseline={editing} />
      <ResultsDialog baseline={resultsFor} onOpenChange={(o) => !o && setResultsFor(null)} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete baseline"
        description={(
          <>Delete <span className="font-semibold text-text">{deleting?.name}</span>? Its rules and
            compliance results will be removed.</>
        )}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

// ── Form dialog ──────────────────────────────────────────────────────

type RuleDraft = {
  rule_type: BaselineRuleType
  package_match: string
  match_type: BaselineMatchType
  min_version: string
  severity: Severity
}

const emptyRule = (): RuleDraft => ({
  rule_type: 'required',
  package_match: '',
  match_type: 'contains',
  min_version: '',
  severity: 'warning',
})

const toDraft = (r: BaselineRule): RuleDraft => ({
  rule_type: r.rule_type,
  package_match: r.package_match,
  match_type: r.match_type,
  min_version: r.min_version || '',
  severity: r.severity,
})

const RULES_GRID = 'grid grid-cols-[96px_minmax(0,1fr)_100px_104px_100px_28px] items-center gap-2'

function BaselineFormDialog({
  open,
  onOpenChange,
  baseline,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** When provided, edits in place; otherwise creates. */
  baseline?: Baseline | null
}) {
  const qc = useQueryClient()
  const editing = Boolean(baseline)

  const [name, setName] = useState('')
  const [osScope, setOsScope] = useState('any')
  const [tags, setTags] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [alerting, setAlerting] = useState(true)
  const [description, setDescription] = useState('')
  const [rules, setRules] = useState<RuleDraft[]>([emptyRule()])

  useEffect(() => {
    if (open) {
      setName(baseline?.name || '')
      setOsScope(baseline?.os_type || 'any')
      setTags((baseline?.match_tags || []).join(', '))
      setEnabled(baseline ? baseline.enabled : true)
      setAlerting(baseline ? baseline.alerting : true)
      setDescription(baseline?.description || '')
      setRules(baseline?.rules.length ? baseline.rules.map(toDraft) : [emptyRule()])
    }
  }, [open, baseline])

  const updateRule = (i: number, patch: Partial<RuleDraft>) =>
    setRules((rs) => rs.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))
  const removeRule = (i: number) => setRules((rs) => rs.filter((_, idx) => idx !== i))

  const canSave = Boolean(name.trim()) && rules.some((r) => r.package_match.trim())

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: name.trim(),
        description: description.trim() || null,
        enabled,
        os_type: osScope === 'any' ? null : osScope,
        match_tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        alerting,
        rules: rules
          .filter((r) => r.package_match.trim())
          .map((r): BaselineRuleInput => ({
            rule_type: r.rule_type,
            package_match: r.package_match.trim(),
            match_type: r.match_type,
            min_version: r.rule_type === 'prohibited' ? null : r.min_version.trim() || null,
            severity: r.severity,
          })),
      }
      if (editing && baseline) {
        return (await api.patch(`/server-baselines/${baseline.id}`, body)).data
      }
      return (await api.post('/server-baselines', body)).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Baseline updated' : 'Baseline created')
      qc.invalidateQueries({ queryKey: ['server-baselines'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ClipboardCheck className="h-4 w-4 text-primary" />
            {editing ? 'Edit baseline' : 'New baseline'}
          </DialogTitle>
          <DialogDescription>
            Servers matching the scope are checked against every rule; violations raise alerts when alerting is on.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label>Name *</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Windows server fleet" />
          </div>
          <div className="space-y-1.5">
            <Label>OS scope</Label>
            <Select value={osScope} onValueChange={setOsScope}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="any">Any OS</SelectItem>
                <SelectItem value="windows">Windows</SelectItem>
                <SelectItem value="linux">Linux</SelectItem>
                <SelectItem value="macos">macOS</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="col-span-2 space-y-1.5">
            <Label>Tags match (comma-separated)</Label>
            <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="prod, web-tier" />
            <p className="text-[11px] text-muted">
              Baseline applies only to servers having ALL these tags; empty = all servers
            </p>
          </div>
          <div className="col-span-2 flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Switch id="bl-enabled" checked={enabled} onCheckedChange={setEnabled} />
              <Label htmlFor="bl-enabled">Enabled</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch id="bl-alerting" checked={alerting} onCheckedChange={setAlerting} />
              <Label htmlFor="bl-alerting">Raise alerts</Label>
            </div>
          </div>
          <div className="col-span-2 space-y-1.5">
            <Label>Description</Label>
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
        </div>

        <div className="space-y-2">
          <Label>Rules *</Label>
          <div className={cn(RULES_GRID, 'text-[10px] font-medium uppercase tracking-wider text-muted')}>
            <span>Type</span>
            <span>Package match</span>
            <span>Match</span>
            <span>Min version</span>
            <span>Severity</span>
            <span />
          </div>
          <div className="space-y-2">
            {rules.map((r, i) => (
              <div key={i} className={RULES_GRID}>
                <Select
                  value={r.rule_type}
                  onValueChange={(v) => updateRule(i, {
                    rule_type: v as BaselineRuleType,
                    ...(v === 'prohibited' ? { min_version: '' } : {}),
                  })}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="required">Required</SelectItem>
                    <SelectItem value="prohibited">Prohibited</SelectItem>
                  </SelectContent>
                </Select>
                <Input
                  value={r.package_match}
                  onChange={(e) => updateRule(i, { package_match: e.target.value })}
                  placeholder="e.g. Google Chrome"
                />
                <Select
                  value={r.match_type}
                  onValueChange={(v) => updateRule(i, { match_type: v as BaselineMatchType })}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="contains">Contains</SelectItem>
                    <SelectItem value="exact">Exact</SelectItem>
                    <SelectItem value="regex">Regex</SelectItem>
                  </SelectContent>
                </Select>
                <Input
                  value={r.min_version}
                  onChange={(e) => updateRule(i, { min_version: e.target.value })}
                  placeholder="optional, e.g. 150.0"
                  disabled={r.rule_type === 'prohibited'}
                />
                <Select
                  value={r.severity}
                  onValueChange={(v) => updateRule(i, { severity: v as Severity })}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="info">Info</SelectItem>
                    <SelectItem value="warning">Warning</SelectItem>
                    <SelectItem value="critical">Critical</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                  onClick={() => removeRule(i)}
                  title="Remove rule"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={() => setRules((rs) => [...rs, emptyRule()])}>
            <Plus className="h-3.5 w-3.5" />
            Add rule
          </Button>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!canSave || save.isPending}>
            {save.isPending ? 'Saving…' : editing ? 'Save changes' : 'Create baseline'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── Results dialog ───────────────────────────────────────────────────

function ResultsDialog({
  baseline,
  onOpenChange,
}: {
  baseline: Baseline | null
  onOpenChange: (open: boolean) => void
}) {
  const { data, isLoading } = useQuery({
    queryKey: ['server-baselines', baseline?.id, 'results'],
    queryFn: async () =>
      (await api.get(`/server-baselines/${baseline?.id}/results`)).data as { items: ComplianceResult[] },
    enabled: !!baseline,
  })
  const results = data?.items || []

  const counts: Record<ComplianceStatus, number> = { compliant: 0, missing: 0, outdated: 0, prohibited: 0 }
  for (const r of results) counts[r.status] += 1

  return (
    <Dialog open={!!baseline} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ListChecks className="h-4 w-4 text-primary" />
            {baseline?.name} — compliance results
          </DialogTitle>
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            {(Object.keys(RESULT_STATUS_META) as ComplianceStatus[])
              .filter((s) => counts[s] > 0)
              .map((s) => (
                <Badge key={s} variant={RESULT_STATUS_META[s].variant}>
                  {counts[s]} {RESULT_STATUS_META[s].label.toLowerCase()}
                </Badge>
              ))}
          </div>
        </DialogHeader>

        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
          </div>
        ) : results.length === 0 ? (
          <div className="py-10 text-center text-sm text-muted">
            No servers evaluated yet — check the baseline scope or wait for the next inventory upload
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-auto rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Server</Th>
                  <Th>Rule</Th>
                  <Th>Status</Th>
                  <Th>Found</Th>
                  <Th>Expected</Th>
                  <Th>Since</Th>
                </Tr>
              </THead>
              <TBody>
                {results.map((r, i) => {
                  const meta = RESULT_STATUS_META[r.status]
                  return (
                    <Tr key={`${r.server_id || i}-${r.rule_id}`}>
                      <Td>
                        <div className="font-medium">{r.server_name || r.hostname || '—'}</div>
                      </Td>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <span className="font-mono text-xs">{r.package_match}</span>
                          <Badge variant={r.rule_type === 'prohibited' ? 'outline' : 'info'}>
                            {r.rule_type}
                          </Badge>
                        </div>
                      </Td>
                      <Td><Badge variant={meta.variant}>{meta.label}</Badge></Td>
                      <Td className="text-xs">
                        {r.found_package
                          ? `${r.found_package}${r.found_version ? ` ${r.found_version}` : ''}`
                          : '—'}
                      </Td>
                      <Td className="text-xs text-muted">{r.expected || '—'}</Td>
                      <Td className="text-xs text-muted">
                        {r.first_failed_at ? relativeTime(r.first_failed_at) : '—'}
                      </Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
