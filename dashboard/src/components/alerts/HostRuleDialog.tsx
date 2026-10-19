/**
 * Server (host_*) alert rule editor: create, edit and duplicate.
 *
 * Opened from a server's Alerts tab (create/edit), its Services tab (create,
 * pre-filled) and the Alert Manager (edit/duplicate of host_* rules, which the
 * generic wizard cannot save). Edit prefills from GET /host-alert-rules (the
 * generic list lacks server_id and the reset level) and saves with PUT
 * /host-alert-rules/{id}. A failed save keeps the dialog open with every field
 * error listed until the user edits.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, apiFieldErrors, cn, type ApiFieldError } from '@/lib/utils'
import { usePolicies } from '@/features/escalation/api'
import { PRIORITIES, PRIORITY_META, derivePriority, effectivePriority, policyForPriority } from '@/features/escalation/model'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { mergeWarnings, ruleNameWarnings, warningsFromApi } from '@/components/forms/alertRuleChecks'
import { FieldErrorBanner, RuleWarnings } from './RuleFeedback'
import {
  DEFAULT_CLEAR_SECONDS, HOST_METRICS, OP_SYMBOL, autoReset, emptyHostRuleForm, fmtDuration, hostMetricKind, hostRuleFormFromRule,
  hostRuleRequest, metricLabel, ruleClearing, validateHostRuleForm, type HostRule, type HostRuleForm, type HostRuleMode, type HostRulePrefill,
} from './hostRuleModel'

/** Rows from GET /host-alert-rules carry auto_reset_threshold; a generic /alert-rules row does not. */
const isHostListRow = (r: any) => !!r && 'auto_reset_threshold' in r && 'server_id' in r

type HostRuleDialogProps = {
  open: boolean
  onOpenChange: (v: boolean) => void
  onSaved?: () => void
  /** The server the dialog was opened for (null on the Alert Manager). */
  serverId?: string | null
  initial?: HostRulePrefill
  /** The rule to edit or copy; a generic /alert-rules row is completed from GET /host-alert-rules. */
  rule?: HostRule | Record<string, any> | null
  mode?: HostRuleMode
}

export function HostRuleDialog(props: HostRuleDialogProps) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent className="max-w-lg">
        {props.open && <HostRuleEditor {...props} />}
      </DialogContent>
    </Dialog>
  )
}

/** The dialog body (exported so scripts/test-alert-rule-editing.mjs can render it without a portal). */
export function HostRuleEditor({
  open, onOpenChange, onSaved, serverId = null, initial, rule = null, mode: modeProp,
}: HostRuleDialogProps) {
  const qc = useQueryClient()
  const mode: HostRuleMode = modeProp ?? (rule?.id ? 'edit' : 'create')
  const needsFetch = open && !!rule?.id && !isHostListRow(rule)
  const listQ = useQuery<{ items: HostRule[] }>({
    queryKey: ['host-alert-rules', 'all'],
    queryFn: async () => (await api.get('/host-alert-rules')).data,
    enabled: needsFetch,
    staleTime: 10_000,
  })
  // Never fall back to the generic /alert-rules row: it lacks server_id, the reset level and the clear hold,
  // so saving it would overwrite them. If the full rule cannot be loaded, say so and do not save.
  const source: HostRule | null = rule?.id
    ? (isHostListRow(rule) ? rule as HostRule : listQ.data?.items?.find((x) => x.id === rule.id) ?? null)
    : null
  const loading = needsFetch && !source && listQ.isLoading
  const loadError = needsFetch && !source
    ? listQ.isError
      ? `Could not load this server rule from Servers → Alerts: ${apiErrorMessage(listQ.error)}. Close the dialog and try again.`
      : listQ.isSuccess ? 'This server rule was not found under Servers → Alerts; it may have been deleted.' : null
    : null

  const [f, setF] = useState<HostRuleForm>(() => (source ? hostRuleFormFromRule(source, mode, serverId) : emptyHostRuleForm(serverId, initial)))
  // The failed save and the form it was made from: the banner shows until the form changes.
  const [failure, setFailure] = useState<{ errors: ApiFieldError[]; form: HostRuleForm } | null>(null)
  const [triedLocal, setTriedLocal] = useState(false)
  const policies = usePolicies(open)

  // Seed the form each time the dialog opens (and once the full rule has loaded).
  const seedKey = `${open}:${source?.id ?? ''}:${mode}:${JSON.stringify(initial ?? null)}`
  useEffect(() => {
    if (!open) return
    setFailure(null)
    setTriedLocal(false)
    setF(source ? hostRuleFormFromRule(source, mode, serverId) : emptyHostRuleForm(serverId, initial))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey])

  const { data: chResp } = useQuery<{ data: { id: string; name: string; type: string }[] }>({
    queryKey: ['notification-channels'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })
  const allChannels = chResp?.data || []

  const kind = hostMetricKind(f.metric)
  const localErrors = useMemo(() => validateHostRuleForm(f), [f])
  // Value-domain problems show as soon as they are typed; a missing service/process name after the first save.
  const fieldError = (path: string) => localErrors.find((e) => e.path === path)?.msg
  const warnings = useMemo(() => ruleNameWarnings({
    name: f.name, description: f.description,
    conditions: kind === 'pct' ? [{ metric: f.metric, operator: f.operator, threshold: Number(f.threshold) }] : [{ metric: f.metric, operator: f.operator, threshold: 0 }],
  }), [f.name, f.description, f.metric, f.operator, f.threshold, kind])
  const banner = failure && failure.form === f ? failure.errors : []

  const save = useMutation({
    mutationFn: async (form: HostRuleForm) => {
      const req = hostRuleRequest(mode, form, source?.id ?? rule?.id)
      return (req.method === 'put' ? await api.put(req.url, req.body) : await api.post(req.url, req.body)).data
    },
    onSuccess: (data) => {
      toast.success(mode === 'edit' ? 'Alert rule updated' : 'Alert rule created')
      const server = warningsFromApi(data)
      if (server.length) toast.warning('Saved with warnings', server.map((w) => w.message).join(' '))
      qc.invalidateQueries({ queryKey: ['host-alert-rules'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onSaved?.()
      onOpenChange(false)
    },
    // The dialog stays open; each field error is listed until the user edits.
    onError: (e, form) => setFailure({ errors: apiFieldErrors(e), form }),
  })

  const submit = () => {
    if (loadError || loading) return
    setTriedLocal(true)
    if (localErrors.length) {
      setFailure({ errors: localErrors, form: f })
      return
    }
    save.mutate(f)
  }

  const set = (patch: Partial<HostRuleForm>) => setF((x) => ({ ...x, ...patch }))
  const toggleChannel = (id: string) => set({ channels: f.channels.includes(id) ? f.channels.filter((x) => x !== id) : [...f.channels, id] })

  const autoLevel = autoReset(f.operator, Number(f.threshold))
  const preview = kind === 'svc'
    ? `Service ${f.target.trim() || '…'} stopped`
    : kind === 'proc'
      ? `Process ${f.target.trim() || '…'} not running`
      : `${metricLabel(f.metric)} ${OP_SYMBOL[f.operator]} ${f.threshold || '…'}% ${fmtDuration(Math.round(Number(f.minutes || 0) * 60))}`
  const clearPreview = kind === 'pct'
    ? ruleClearing({
        operator: f.operator, threshold: Number(f.threshold),
        reset_threshold: f.resetLevel.trim() !== '' ? Number(f.resetLevel) : null,
        auto_reset_threshold: autoLevel,
        clear_duration: f.clearMinutes.trim() !== '' ? Math.round(Number(f.clearMinutes) * 60) : null,
      })
    : ''
  const scopeText = f.allServers || !f.scopeServerId ? 'any server' : f.scopeServerId === serverId ? 'this server' : 'its server'
  const title = mode === 'edit' ? 'Edit server alert rule' : mode === 'duplicate' ? 'Duplicate server alert rule' : 'New alert rule'

  return (
    <>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {mode === 'edit'
              ? `Server rules are saved under Servers → Alerts. Scope: ${f.allServers || !f.scopeServerId ? 'all servers' : f.scopeServerId === serverId ? 'this server' : 'one server'} (the scope is fixed once created).`
              : mode === 'duplicate' ? 'The copy is created disabled unless you enable it here.' : 'Alerts on CPU, memory, filesystem or disk thresholds, or on a stopped service or process.'}
          </DialogDescription>
        </DialogHeader>
        {loading ? (
          <div className="flex items-center gap-2 py-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading the rule…</div>
        ) : loadError ? (
          <>
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
              {loadError}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="button" disabled>{mode === 'edit' ? 'Save rule' : 'Create rule'}</Button>
            </DialogFooter>
          </>
        ) : (
          <form
            className="space-y-3"
            noValidate
            onSubmit={(e) => { e.preventDefault(); submit() }}
          >
            <FieldErrorBanner errors={banner} />
            <div className="space-y-1.5">
              <Label htmlFor="host-rule-name">Name</Label>
              <Input id="host-rule-name" value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder={`e.g. ${metricLabel(f.metric)} alert`} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="host-rule-description">Description</Label>
              <Textarea id="host-rule-description" rows={2} value={f.description} onChange={(e) => set({ description: e.target.value })} placeholder="Optional" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Metric</Label>
                <Select value={f.metric} onValueChange={(v) => set({ metric: v, ...(v === 'host_filesystem_pct' && mode === 'create' ? { threshold: '85' } : {}) })}>
                  <SelectTrigger aria-label="Metric"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {HOST_METRICS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Severity</Label>
                <Select value={f.severity} onValueChange={(v) => set({ severity: v })}>
                  <SelectTrigger aria-label="Severity"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="info">Info</SelectItem>
                    <SelectItem value="warning">Warning</SelectItem>
                    <SelectItem value="critical">Critical</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Priority</Label>
                <Select value={f.priority} onValueChange={(v) => set({ priority: v })}>
                  <SelectTrigger aria-label="Incident priority"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">Auto from severity ({derivePriority(f.severity)})</SelectItem>
                    {PRIORITIES.map((p) => <SelectItem key={p} value={p}>{PRIORITY_META[p].label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Escalation</Label>
                <Select value={f.policyId} onValueChange={(v) => set({ policyId: v })}>
                  <SelectTrigger aria-label="Escalation policy"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="default">
                      {(() => {
                        const eff = effectivePriority(f.priority === 'auto' ? null : f.priority, f.severity)
                        const d = policyForPriority(policies.data ?? [], eff)
                        return d ? `${eff} default (${d.name})` : `${eff} default (none set)`
                      })()}
                    </SelectItem>
                    {(policies.data ?? []).map((p) => <SelectItem key={p.id} value={p.id}>{p.name}{p.enabled ? '' : ' (disabled)'}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {kind === 'pct' ? (
              <div className="grid grid-cols-3 gap-3">
                <div className="space-y-1.5">
                  <Label>Condition</Label>
                  <Select value={f.operator} onValueChange={(v) => set({ operator: v })}>
                    <SelectTrigger aria-label="Condition"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="gt">Above (&gt;)</SelectItem>
                      <SelectItem value="gte">At least (≥)</SelectItem>
                      <SelectItem value="lt">Below (&lt;)</SelectItem>
                      <SelectItem value="lte">At most (≤)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="host-rule-threshold">Threshold %</Label>
                  <Input id="host-rule-threshold" type="number" min={0} max={100} step="any" value={f.threshold}
                    aria-invalid={!!fieldError('threshold')} onChange={(e) => set({ threshold: e.target.value })} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="host-rule-minutes">For (min)</Label>
                  <Input id="host-rule-minutes" type="number" min={0} value={f.minutes} onChange={(e) => set({ minutes: e.target.value })} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="host-rule-reset">Clear level %</Label>
                  <Input
                    id="host-rule-reset" type="number" min={0} max={100} step="any" value={f.resetLevel}
                    aria-invalid={!!fieldError('reset_threshold')}
                    onChange={(e) => set({ resetLevel: e.target.value })}
                    placeholder={autoLevel != null ? `auto (${autoLevel}%)` : 'same as threshold'}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="host-rule-clear">Clear after (min)</Label>
                  <Input
                    id="host-rule-clear" type="number" min={0} value={f.clearMinutes}
                    onChange={(e) => set({ clearMinutes: e.target.value })}
                    placeholder={String(DEFAULT_CLEAR_SECONDS / 60)}
                  />
                </div>
                <label className="flex cursor-pointer items-end gap-2 pb-2">
                  <Switch checked={f.recovery} onCheckedChange={(v) => set({ recovery: v })} />
                  <span className="text-[13px]">Recovery notice</span>
                </label>
                {(['threshold', 'reset_threshold', 'min_duration', 'clear_duration'] as const).map((p) => fieldError(p)
                  ? <p key={p} className="col-span-3 text-[11px] text-danger">{fieldError(p)}</p> : null)}
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="host-rule-target">{kind === 'svc' ? 'Service name' : 'Process name'}</Label>
                <Input
                  id="host-rule-target"
                  value={f.target}
                  aria-invalid={triedLocal && !!localErrors.find((e) => e.path === 'target')}
                  onChange={(e) => set({ target: e.target.value })}
                  placeholder={kind === 'svc' ? 'e.g. MSSQLSERVER' : 'e.g. nginx.exe'}
                />
                {triedLocal && localErrors.find((e) => e.path === 'target') && <p className="text-[11px] text-danger">{localErrors.find((e) => e.path === 'target')!.msg}</p>}
              </div>
            )}

            <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
              Alerts when <span className="font-medium text-text">{preview}</span> on{' '}
              <span className="font-medium text-text">{scopeText}</span>.
              {clearPreview && (
                <> One alert per {f.metric === 'host_filesystem_pct' ? 'filesystem' : 'server'} until it{' '}
                  <span className="font-medium text-text">{clearPreview}</span>; missing data never clears it.</>
              )}
            </div>

            <RuleWarnings warnings={mergeWarnings(warnings)} />

            {mode !== 'edit' && (
              <label className={cn('flex items-center gap-2', f.scopeServerId ? 'cursor-pointer' : 'opacity-70')}>
                <Switch checked={f.allServers || !f.scopeServerId} disabled={!f.scopeServerId} onCheckedChange={(v) => set({ allServers: v })} />
                <span className="text-[13px]">{f.scopeServerId ? 'Apply to all servers (not just this one)' : 'Applies to all servers'}</span>
              </label>
            )}
            <label className="flex cursor-pointer items-center gap-2">
              <Switch checked={f.enabled} onCheckedChange={(v) => set({ enabled: v })} />
              <span className="text-[13px]">Enabled</span>
            </label>

            {allChannels.length > 0 && (
              <div className="space-y-1.5">
                <Label>Notify channels</Label>
                <div className="flex flex-wrap gap-1.5">
                  {allChannels.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => toggleChannel(c.id)}
                      aria-pressed={f.channels.includes(c.id)}
                      className={cn(
                        'rounded-md border px-2 py-1 text-xs transition-colors',
                        f.channels.includes(c.id)
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-border text-muted hover:text-text',
                      )}
                    >
                      {c.name} <span className="opacity-60">({c.type})</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? 'Saving…' : mode === 'edit' ? 'Save rule' : 'Create rule'}
              </Button>
            </DialogFooter>
          </form>
        )}
    </>
  )
}
