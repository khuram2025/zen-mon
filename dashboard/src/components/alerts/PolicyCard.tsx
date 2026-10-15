/**
 * Effective policy for an alert (A05, A20–A22, F08).
 *
 * - Full rule summary: conditions with reset thresholds, logic, trigger, hold
 *   time (exact), cooldown, schedule, recovery, escalation, scope and the
 *   population a change affects ("Shared policy — affects N devices").
 * - Differences between the rule as it was when the alert fired
 *   (`policy_snapshot`) and today's rule are listed.
 * - Rule controls only when `rule.editable` and alerts.manage. Enabling or
 *   disabling goes through an impact-confirming dialog; the quick editor seeds
 *   once per open, preserves every other condition property and sends only
 *   the fields that changed.
 */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, History, Loader2, Pencil, Power, TriangleAlert, Users } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { SeverityBadge, focusRing } from './AlertBits'
import { fmtDurExact, fmtMinutes } from './format'
import type { AlertRuleDetail, RuleCondition, RuleImpact } from './types'

const OPERATORS = ['>', '>=', '<', '<=', '==', '!=']

function condText(c: RuleCondition): string {
  const base = `${c.metric} ${c.operator} ${c.threshold ?? '—'}`
  return c.reset_threshold != null ? `${base} (resets at ${c.reset_threshold})` : base
}

/** schedule_days are ISO weekdays (1 = Monday … 7 = Sunday), as the engine reads them. */
const ISO_DAYS = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function scheduleText(s: AlertRuleDetail['schedule']): string {
  if (!s) return 'Always (no schedule)'
  const nums = (s.days ?? []).map((d) => Number(d)).filter((d) => d >= 1 && d <= 7)
  const days = nums.length === 0 || nums.length === 7 ? 'every day' : nums.sort().map((d) => ISO_DAYS[d]).join(', ')
  const hours = s.start && s.end ? `${s.start}–${s.end}` : 'all day'
  return `Notifies ${hours}, ${days} (appliance timezone)`
}

function triggerText(t: AlertRuleDetail['trigger_on']): string | null {
  switch (t) {
    case 'down': return 'When the target goes down'
    case 'degraded': return 'When the target degrades'
    case 'up': return 'When the target comes up'
    case 'any': return 'On any status change'
    default: return null
  }
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_1fr] gap-2 py-1 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

// Metrics a device rule reads from a confirmed status change; only those rules are held while a device flaps.
const STATUS_EVENT_METRICS = new Set(['ping_status', 'rtt', 'packet_loss'])

/* ── Snapshot diff (F08) ──────────────────────────────────────────────── */

const DIFF_FIELDS: Array<{ key: keyof AlertRuleDetail; label: string; fmt?: (v: any) => string }> = [
  { key: 'name', label: 'Name' },
  { key: 'enabled', label: 'State', fmt: (v) => (v ? 'Enabled' : 'Disabled') },
  { key: 'severity', label: 'Severity' },
  { key: 'condition_label', label: 'Condition' },
  { key: 'conditions', label: 'Conditions', fmt: (v) => (Array.isArray(v) ? v.map(condText).join(' · ') : '—') },
  { key: 'condition_logic', label: 'Logic' },
  { key: 'threshold', label: 'Threshold' },
  { key: 'operator', label: 'Operator' },
  { key: 'min_duration', label: 'Hold time', fmt: (v) => (v ? fmtDurExact(v) : 'Immediate') },
  { key: 'cooldown', label: 'Cooldown', fmt: (v) => (v ? fmtDurExact(v) : 'None') },
  { key: 'trigger_on', label: 'Trigger' },
  { key: 'notify_channels', label: 'Channels', fmt: (v) => (Array.isArray(v) ? `${v.length} channel${v.length === 1 ? '' : 's'}` : '—') },
  { key: 'scope', label: 'Scope', fmt: (v) => v?.label ?? '—' },
]

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`
  if (v && typeof v === 'object') return `{${Object.keys(v as object).sort().map((k) => `${k}:${stable((v as any)[k])}`).join(',')}}`
  return JSON.stringify(v ?? null)
}

function snapshotDiff(snap: Partial<AlertRuleDetail> | null, rule: AlertRuleDetail | null) {
  if (!snap || !rule) return []
  const out: Array<{ label: string; then: string; now: string }> = []
  for (const f of DIFF_FIELDS) {
    if (!(f.key in snap)) continue
    const a = snap[f.key]
    const b = rule[f.key]
    const cmpA = f.key === 'scope' ? (a as any)?.label : a
    const cmpB = f.key === 'scope' ? (b as any)?.label : b
    if (stable(cmpA) === stable(cmpB)) continue
    const fmt = f.fmt ?? ((v: unknown) => (v == null ? '—' : String(v)))
    out.push({ label: f.label, then: fmt(a), now: fmt(b) })
  }
  return out
}

/* ── Card ─────────────────────────────────────────────────────────────── */

export function PolicyCard({
  rule, snapshot,
}: {
  rule: AlertRuleDetail | null
  snapshot: Partial<AlertRuleDetail> | null
}) {
  const can = useCan()
  const [editOpen, setEditOpen] = useState(false)
  const [toggleOpen, setToggleOpen] = useState(false)
  const diff = useMemo(() => snapshotDiff(snapshot, rule), [snapshot, rule])

  if (!rule) {
    return (
      <Card>
        <CardContent className="p-4">
          <h2 className="mb-2 text-base font-semibold">Policy</h2>
          <p className="text-sm text-muted">
            {snapshot?.name ? <>Raised by rule “{snapshot.name}”, which no longer exists.</> : 'No alert rule is linked — this alert came from its producer directly.'}
          </p>
        </CardContent>
      </Card>
    )
  }

  const canEdit = rule.editable && can('alerts.manage')
  const conditions = rule.conditions?.length ? rule.conditions : [{ metric: rule.metric, operator: rule.operator, threshold: rule.threshold } as RuleCondition]
  const compound = (rule.conditions?.length ?? 0) > 1
  const affected = rule.affected
  const trigger = triggerText(rule.trigger_on)
  // Device status rules (evaluated on confirmed status changes) wait out a flapping device unless they opt out; an 'up'
  // rule's trigger is a recovery, which is never held.
  const flapHold = typeof rule.notify_while_flapping === 'boolean' && rule.trigger_on !== 'up'
    && conditions.some((c) => STATUS_EVENT_METRICS.has(c.metric))

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="text-base font-semibold">Policy</h2>
            <p className="text-xs text-muted">Current rule — changes affect every target in scope, not just this incident.</p>
          </div>
          {canEdit && (
            <div className="flex shrink-0 items-center gap-1">
              <Button size="sm" variant="outline" className={cn('h-8', focusRing)} onClick={() => setToggleOpen(true)} aria-label={`${rule.enabled ? 'Disable' : 'Enable'} rule ${rule.name}`}>
                <Power className={cn('h-3.5 w-3.5', rule.enabled ? 'text-success' : 'text-muted')} /> {rule.enabled ? 'Disable' : 'Enable'}
              </Button>
              {compound ? (
                <Button size="sm" variant="outline" className={cn('h-8', focusRing)} asChild>
                  <Link to="/alert-rules" aria-label={`Edit compound rule ${rule.name} in Alert Rules`}><Pencil className="h-3.5 w-3.5" /> Edit</Link>
                </Button>
              ) : (
                <Button size="sm" variant="outline" className={cn('h-8', focusRing)} onClick={() => setEditOpen(true)} aria-label={`Edit rule ${rule.name}`}>
                  <Pencil className="h-3.5 w-3.5" /> Edit
                </Button>
              )}
            </div>
          )}
        </div>

        {affected?.shared && (
          <div className="mb-2 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-2.5 py-2 text-sm text-warning">
            <Users className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span>Shared policy — affects {affected.count != null ? affected.count.toLocaleString() : 'several'} {affected.noun}. Editing or disabling it changes alerting for all of them.</span>
          </div>
        )}

        {diff.length > 0 && (
          <details className="mb-2 rounded-md border border-info/40 bg-info/5 px-2.5 py-2 text-sm">
            <summary className={cn('flex cursor-pointer items-center gap-1.5 font-medium text-info', focusRing)}>
              <History className="h-4 w-4" aria-hidden /> Rule changed since this alert fired ({diff.length})
            </summary>
            <ul className="mt-1.5 space-y-1">
              {diff.map((d) => (
                <li key={d.label}>
                  <span className="text-muted">{d.label}:</span> <span className="line-through decoration-muted/60">{d.then}</span> → <span className="font-medium">{d.now}</span>
                </li>
              ))}
            </ul>
          </details>
        )}

        <dl className="divide-y divide-border/60">
          <Row label="Rule">
            <span className="font-medium">{rule.name}</span>{' '}
            <span className={cn('ml-1 rounded px-1.5 py-px text-xs font-medium', rule.enabled ? 'bg-surface2 text-text2' : 'bg-warning/10 text-warning')}>
              {rule.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </Row>
          <Row label="Severity"><SeverityBadge severity={rule.severity} /></Row>
          <Row label="Condition">
            {rule.condition_label && <div className="font-medium">{rule.condition_label}</div>}
            {(compound || conditions.some((c) => c.reset_threshold != null) || !rule.condition_label) && (
              <ul className="mt-0.5 space-y-0.5 font-mono text-xs">
                {conditions.map((c, i) => (
                  <li key={i}>
                    {i > 0 && <span className="mr-1 font-sans font-semibold text-muted">{rule.condition_logic || 'AND'}</span>}
                    {condText(c)}
                  </li>
                ))}
              </ul>
            )}
          </Row>
          {trigger && <Row label="Trigger">{trigger}</Row>}
          <Row label="Hold time">{rule.min_duration ? `${fmtDurExact(rule.min_duration)} (${rule.min_duration.toLocaleString()} s)` : 'Immediate'}</Row>
          {flapHold && (
            <Row label="While flapping">
              {rule.notify_while_flapping ? 'Notifies immediately, even while the device is flapping' : 'Held until a status lasts the flap stability time'}
            </Row>
          )}
          <Row label="Cooldown">{rule.cooldown ? fmtDurExact(rule.cooldown) : 'None'}{rule.max_repeat ? ` · max ${rule.max_repeat} repeats` : ''}</Row>
          <Row label="Schedule">{scheduleText(rule.schedule)}</Row>
          <Row label="Recovery">{rule.recovery_alert ? 'Sends a recovery notice when the condition clears' : 'No recovery notice'}</Row>
          <Row label="Escalation">
            {rule.escalation_levels?.length ? (
              <ol className="space-y-0.5">
                {rule.escalation_levels.map((l, i) => (
                  <li key={i}>
                    <span className="text-muted">L{i + 1}</span> after {fmtMinutes(l.after_minutes)} → {l.channel_names?.length ? l.channel_names.join(', ') : `${l.notify_channels.length} channel(s)`}
                    {l.repeat_every_minutes ? <span className="text-muted"> · repeats every {fmtMinutes(l.repeat_every_minutes)}</span> : null}
                  </li>
                ))}
              </ol>
            ) : 'No escalation levels'}
          </Row>
          <Row label="Scope">
            {rule.scope?.label ?? '—'}
            {affected && (
              <div className="text-xs text-muted">
                {affected.count != null ? `${affected.count.toLocaleString()} ${affected.noun}` : `Unknown number of ${affected.noun}`} in scope
              </div>
            )}
          </Row>
        </dl>

        {!canEdit && (
          <p className="mt-2 text-xs text-muted">
            Managed in <Link to="/alert-rules" className={cn('text-primary hover:underline', focusRing)}>Alert Rules</Link>
            {can('alerts.manage') && !rule.editable ? ' — this rule is outside your scope (fleet-wide changes need unrestricted access).' : '.'}
          </p>
        )}
        {canEdit && (
          <p className="mt-2 text-xs text-muted">
            Full policy (templates, targets, all conditions) in <Link to="/alert-rules" className={cn('inline-flex items-center gap-0.5 text-primary hover:underline', focusRing)}>Alert Rules <ExternalLink className="h-3 w-3" /></Link>
          </p>
        )}
      </CardContent>

      {canEdit && <RuleToggleDialog open={toggleOpen} onOpenChange={setToggleOpen} rule={rule} />}
      {canEdit && !compound && editOpen && (
        <RuleEditDialog key={rule.id} onClose={() => setEditOpen(false)} rule={rule} />
      )}
    </Card>
  )
}

/* ── Toggle with impact (A22) ─────────────────────────────────────────── */

function RuleToggleDialog({ open, onOpenChange, rule }: { open: boolean; onOpenChange: (v: boolean) => void; rule: AlertRuleDetail }) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  const disabling = rule.enabled
  const impact = useQuery<RuleImpact>({
    queryKey: ['alert-rules', rule.id, 'impact'],
    queryFn: async () => (await api.get(`/alert-rules/${rule.id}/impact`)).data,
    enabled: open,
    staleTime: 0,
  })
  const toggle = useMutation({
    mutationFn: async () =>
      (await api.post(`/alert-rules/${rule.id}/toggle`, { enabled: !rule.enabled, ...(reason.trim() ? { reason: reason.trim() } : {}) })).data,
    onSuccess: (d: any) => {
      const enabled = d?.enabled ?? !rule.enabled
      toast.success(enabled ? 'Rule enabled' : 'Rule disabled', enabled ? 'Evaluation resumes on the next cycle.' : 'No new alerts will be raised by this rule.')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
      setReason('')
    },
    onError: (e) => toast.error('Could not change the rule', apiErrorMessage(e)),
  })
  const i = impact.data
  const scopeLabel = i ? (typeof i.scope === 'string' ? i.scope : i.scope?.label) : rule.scope?.label
  const aff = i?.affected ?? rule.affected

  return (
    <Dialog open={open} onOpenChange={(v) => { onOpenChange(v); if (!v) setReason('') }}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {disabling && <TriangleAlert className="h-5 w-5 text-warning" aria-hidden />}
            {disabling ? 'Disable' : 'Enable'} rule “{rule.name}”?
          </DialogTitle>
          <DialogDescription>
            This changes the policy, not this incident.
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-md border border-border bg-surface2/40 p-3 text-sm">
          {impact.isLoading ? (
            <div className="flex items-center gap-2 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Calculating impact…</div>
          ) : impact.isError ? (
            <div className="text-danger">Impact unavailable: {apiErrorMessage(impact.error)}. Proceed only if you know the rule's scope.</div>
          ) : (
            <ul className="space-y-1">
              <li><span className="text-muted">Scope:</span> {scopeLabel ?? '—'}</li>
              <li>
                <span className="text-muted">Targets:</span>{' '}
                {aff?.count != null ? `${aff.count.toLocaleString()} ${aff.noun}` : 'unknown'}{aff?.shared ? ' (shared policy)' : ''}
              </li>
              {i && <li><span className="text-muted">Open alerts from this rule:</span> {i.open_alerts.toLocaleString()}</li>}
              <li className="pt-1">
                {disabling
                  ? `No ${aff?.noun ?? 'targets'} in scope will raise new alerts from this rule until it is re-enabled. Existing open alerts stay open for review (Backlog health → Rule disabled).`
                  : 'Evaluation resumes for every target in scope; conditions already breaching may alert immediately.'}
              </li>
            </ul>
          )}
        </div>
        <div>
          <label htmlFor="toggle-reason" className="text-sm font-medium">Reason <span className="font-normal text-muted">(optional, audited)</span></label>
          <Textarea id="toggle-reason" className="mt-1" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={disabling ? 'e.g. noisy during migration, CHG-1234' : ''} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={toggle.isPending}>Cancel</Button>
          <Button variant={disabling ? 'destructive' : 'default'} onClick={() => toggle.mutate()} disabled={toggle.isPending || impact.isLoading}>
            {toggle.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {disabling ? 'Disable rule' : 'Enable rule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Quick editor (A21) ───────────────────────────────────────────────── */

/**
 * Mounted only while open, so its state is seeded exactly once per opening
 * from the rule as it was at that moment — background refetches never
 * overwrite what the user is typing.
 */
function RuleEditDialog({ onClose, rule: live }: { onClose: () => void; rule: AlertRuleDetail }) {
  const qc = useQueryClient()
  const [base] = useState(live)
  const cond0: RuleCondition | null = base.conditions?.length === 1 ? base.conditions[0] : null
  const initOp = cond0?.operator ?? base.operator ?? '>'
  const initThr = cond0?.threshold ?? base.threshold
  const [name, setName] = useState(base.name)
  const [severity, setSeverity] = useState(String(base.severity || 'warning'))
  const [operator, setOperator] = useState(initOp)
  const [threshold, setThreshold] = useState(initThr == null ? '' : String(initThr))
  const [hold, setHold] = useState(String(base.min_duration ?? 0))

  const thrNum = threshold.trim() === '' ? null : Number(threshold)
  const holdNum = Number(hold)
  const thrValid = thrNum === null ? initThr == null : Number.isFinite(thrNum)
  const holdValid = Number.isInteger(holdNum) && holdNum >= 0 && holdNum <= 30 * 86400
  const nameValid = name.trim().length > 0

  const body = useMemo(() => {
    const b: Record<string, unknown> = {}
    if (name.trim() !== base.name) b.name = name.trim()
    if (severity !== String(base.severity)) b.severity = severity
    if (holdValid && holdNum !== (base.min_duration ?? 0)) b.min_duration = holdNum
    const opDirty = operator !== initOp
    const thrDirty = thrNum !== (initThr ?? null)
    if (opDirty || thrDirty) {
      if (opDirty) b.operator = operator
      if (thrDirty) b.threshold = thrNum
      if (cond0) {
        // Preserve every other property of the stored condition (reset_threshold, units, …).
        b.conditions = [{ ...cond0, operator, threshold: thrNum }]
      }
    }
    return b
  }, [name, severity, operator, thrNum, holdNum, holdValid, base, cond0, initOp, initThr])
  const dirty = Object.keys(body).length > 0
  const valid = nameValid && thrValid && holdValid

  const save = useMutation({
    mutationFn: async () => (await api.put(`/alert-rules/${base.id}`, body)).data,
    onSuccess: () => {
      toast.success('Alert rule updated')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onClose()
    },
    onError: (e) => toast.error('Update failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Pencil className="h-4 w-4 text-primary" aria-hidden /> Edit rule</DialogTitle>
          <DialogDescription>
            {base.affected?.shared
              ? `This rule is shared: changes apply to ${base.affected.count?.toLocaleString() ?? 'all'} ${base.affected.noun}, not just this incident.`
              : 'Changes apply to the rule, not just this incident.'}{' '}
            Only fields you change are saved.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div>
            <label htmlFor="re-name" className="text-sm font-medium">Name</label>
            <Input id="re-name" className="mt-1" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!nameValid} />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="col-span-3 sm:col-span-1">
              <label className="text-sm font-medium" htmlFor="re-metric">Metric</label>
              <Input id="re-metric" className="mt-1 opacity-70" value={cond0?.metric ?? base.metric} disabled />
            </div>
            <div>
              <label className="text-sm font-medium" htmlFor="re-op">Operator</label>
              <Select value={operator} onValueChange={setOperator}>
                <SelectTrigger id="re-op" className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>{OPERATORS.map((op) => <SelectItem key={op} value={op}>{op}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="col-span-2 sm:col-span-1">
              <label className="text-sm font-medium" htmlFor="re-thr">Threshold</label>
              <Input id="re-thr" className="mt-1" type="number" step="any" value={threshold} onChange={(e) => setThreshold(e.target.value)} aria-invalid={!thrValid} />
            </div>
          </div>
          {cond0?.reset_threshold != null && (
            <p className="text-xs text-muted">Reset threshold {cond0.reset_threshold} and other condition settings are kept unchanged.</p>
          )}
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-sm font-medium" htmlFor="re-sev">Severity</label>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger id="re-sev" className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="critical">Critical</SelectItem>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="info">Info</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-sm font-medium" htmlFor="re-hold">Hold time (seconds)</label>
              <Input id="re-hold" className="mt-1" type="number" min={0} step={1} value={hold} onChange={(e) => setHold(e.target.value)} aria-invalid={!holdValid} />
              <span className={cn('text-xs', holdValid ? 'text-muted' : 'text-danger')}>
                {holdValid ? (holdNum ? `= ${fmtDurExact(holdNum)}` : 'Immediate') : 'Whole seconds, up to 30 days'}
              </span>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!dirty || !valid || save.isPending}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {dirty ? 'Save changes' : 'No changes'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
