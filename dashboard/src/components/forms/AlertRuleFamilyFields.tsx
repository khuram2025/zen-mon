/**
 * Alert wizard inputs for the rule families the server gained in the alerting audit (2026-11-19):
 * service-check certificate and response-time metrics, monitoring gaps, the ZenPlus appliance,
 * virtualisation, Device Tracker and NetPath rules, plus the template state picker. The catalogue,
 * validators and payload live in alertRuleCatalog.ts.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { TagPicker } from '@/components/tags/TagPicker'
import {
  VM_ALARM_LEVELS, badCodes, conditionsForStates, knownCodes, metricSpec, metricsFor, newCondition,
  operatorsFor, scopeKeysFor, statesOf, stateWords, type Cond, type RuleSource, type StateInfo,
} from './alertRuleCatalog'

/** A scope field chosen but not picked yet (like the server wizard's one-server placeholder). */
export const PICK = '__pick__'

type Draft = {
  source: string
  conditions: Cond[]
  device_id: string
  group_id: string
  scope_tag: string
  service_check_id: string
  service_check_group_id: string
  target: string
}
/** A change to the draft; never the source. */
type Patch = Partial<Omit<Draft, 'source'>>

/** Metric, operator, limit and clear level for one single-condition family rule. */
export function FamilyConditionFields({ s, onChange, newRule, hideWatch }: {
  s: Draft
  onChange: (patch: Patch) => void
  /** A new rule: the picker hides retired metrics. */
  newRule: boolean
  /** The caller shows its own metric picker (service checks). */
  hideWatch?: boolean
}) {
  const c = s.conditions[0]
  const spec = metricSpec(c?.metric)
  const options = metricsFor(s.source as RuleSource, newRule ? null : c?.metric)
  const setCond = (patch: Partial<Cond>) => onChange({ conditions: [{ ...c, ...patch }] })
  const clearable = !!spec && !spec.fixed && spec.clearLevel !== false && spec.clearLevel !== undefined
  return (
    <div className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-3">
      <div className={cn('grid gap-2', spec?.fixed || spec?.key === 'vm_alarm' ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-1 sm:grid-cols-3')}>
        {!hideWatch && <FormField label="Watch">
          <Select value={c?.metric} onValueChange={(v) => {
            const next = newCondition(v)
            const keep = scopeKeysFor(v)
            // A scope the new metric does not take is dropped (the server refuses it); a target keeps its
            // meaning only within one kind (a channel stays a channel).
            const sameTarget = keep.includes('target') && (metricSpec(v)?.target ?? s.source) === (spec?.target ?? s.source)
            onChange({
              conditions: [next],
              target: sameTarget ? s.target : '',
              device_id: keep.includes('device_id') ? s.device_id : '',
              group_id: keep.includes('group_id') ? s.group_id : '',
              scope_tag: keep.includes('scope_tag') ? s.scope_tag : '',
              service_check_id: keep.includes('service_check_id') ? s.service_check_id : '',
              service_check_group_id: keep.includes('service_check_group_id') ? s.service_check_group_id : '',
            })
          }}>
            <SelectTrigger aria-label="What the rule watches"><SelectValue /></SelectTrigger>
            <SelectContent>
              {options.map((m) => <SelectItem key={m.key} value={m.key}>{m.label}{m.retired ? ' (retired)' : ''}</SelectItem>)}
            </SelectContent>
          </Select>
        </FormField>}
        {spec?.key === 'vm_alarm' ? (
          <FormField label="Alarms">
            <Select value={String(Number(c.threshold) >= 2 ? 2 : 1)} onValueChange={(v) => setCond({ operator: '>=', threshold: Number(v) })}>
              <SelectTrigger aria-label="Which alarms"><SelectValue /></SelectTrigger>
              <SelectContent>
                {VM_ALARM_LEVELS.map((l) => <SelectItem key={l.threshold} value={String(l.threshold)}>{l.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </FormField>
        ) : spec?.fixed ? (
          <p className="self-end pb-2 text-xs text-muted">Fires while this is true; there is no value to compare.</p>
        ) : (
          <>
            <FormField label="Operator">
              <Select value={c.operator} onValueChange={(v) => setCond({ operator: v })}>
                <SelectTrigger aria-label="Operator"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {operatorsFor(c.metric, c.operator).map((op) => <SelectItem key={op} value={op}>{op}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label={`Limit${spec?.unit ? ` (${spec.unit.trim()})` : ''}`}>
              <Input type="number" step="any" min={spec?.min} max={spec?.max} value={c.threshold}
                onChange={(e) => setCond({ threshold: Number(e.target.value) })} aria-label="Limit" />
            </FormField>
          </>
        )}
      </div>
      {clearable && spec?.clearLevel && (
        <FormField label="Clear level (optional)" hint={`Empty = ${spec.clearLevel.auto}. It must lie on the healthy side of the limit.`}>
          <Input type="number" step="any" className="w-40" placeholder="auto" value={c.reset_threshold ?? ''}
            onChange={(e) => setCond({ reset_threshold: e.target.value === '' ? null : Number(e.target.value) })} aria-label="Clear level" />
        </FormField>
      )}
      {spec?.hint && <p className="text-xs text-muted">{spec.hint}</p>}
      {spec?.retired && <p className="text-xs text-warning">{spec.retired}</p>}
    </div>
  )
}

type Option = { id: string; name: string }

function ModeSelect({ label, value, options, onChange }: {
  label: string; value: string; options: Array<{ value: string; label: string }>; onChange: (v: string) => void
}) {
  return (
    <FormField label={label}>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={label}><SelectValue /></SelectTrigger>
        <SelectContent>
          {options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </FormField>
  )
}

function PickOne({ label, value, options, onChange, placeholder }: {
  label: string; value: string; options: Option[]; onChange: (v: string) => void; placeholder: string
}) {
  const v = value || PICK
  return (
    <FormField label={label}>
      <Select value={v} onValueChange={onChange}>
        <SelectTrigger aria-label={label}><SelectValue placeholder={placeholder} /></SelectTrigger>
        <SelectContent>
          <SelectItem value={PICK} disabled>{placeholder}</SelectItem>
          {v !== PICK && !options.some((o) => o.id === v) && <SelectItem value={v}>{v.length > 12 ? `${v.slice(0, 8)}…` : v}</SelectItem>}
          {options.map((o) => <SelectItem key={o.id} value={o.id}>{o.name}</SelectItem>)}
        </SelectContent>
      </Select>
    </FormField>
  )
}

const CLEARED = { device_id: '', group_id: '', scope_tag: '', service_check_id: '', service_check_group_id: '', target: '' }

/**
 * Scope for the families whose scope is not the device one: appliance (channel or sensor), virtualisation
 * (one source, its linked device or a source tag), service-check metrics (check, check group, linked
 * device, tag), agent metric gaps (group or tag) and NetPath (one probe). Returns null for other metrics.
 */
export function FamilyScopeFields({ s, onChange, open, devices, serviceChecks, serviceGroups, deviceGroups, channels }: {
  s: Draft
  onChange: (patch: Patch) => void
  open: boolean
  devices: Array<{ id: string; hostname: string; ip_address?: string | null }>
  serviceChecks: Option[]
  serviceGroups: Option[]
  deviceGroups: Option[]
  channels: Array<{ id: string; name: string; type?: string }>
}) {
  const metric = s.conditions[0]?.metric || ''
  const spec = metricSpec(metric)
  const keys = scopeKeysFor(metric)
  const source = s.source
  const sensorsQ = useQuery<any[]>({
    queryKey: ['sensors', 'list-min'],
    queryFn: async () => (await api.get('/sensors')).data,
    enabled: open && spec?.target === 'sensor',
    staleTime: 60_000,
  })
  const connectorsQ = useQuery<any[]>({
    queryKey: ['virtualization', 'connectors'],
    queryFn: async () => (await api.get('/virtualization/connectors')).data,
    enabled: open && source === 'virtualization',
    staleTime: 60_000,
  })
  const probesQ = useQuery<any>({
    queryKey: ['netpath', 'probes', 'list-min'],
    queryFn: async () => (await api.get('/netpath/probes')).data,
    enabled: open && source === 'netpath',
    staleTime: 60_000,
  })
  const deviceOptions = devices.map((d) => ({ id: d.id, name: `${d.hostname}${d.ip_address ? ` · ${d.ip_address}` : ''}` }))

  if (source === 'appliance') {
    if (!spec?.target) {
      return <p className="rounded-lg border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">Appliance rules watch ZenPlus itself: they have no device, server or tag scope. Only unrestricted users can create or change them.</p>
    }
    const list = spec.target === 'channel'
      ? channels.map((c) => ({ id: c.id, name: c.name }))
      : (Array.isArray(sensorsQ.data) ? sensorsQ.data : []).map((x: any) => ({ id: String(x.id), name: x.name }))
    const mode = s.target ? 'one' : 'all'
    return (
      <div className="space-y-3">
        <ModeSelect label="Apply to" value={mode}
          options={[{ value: 'all', label: spec.target === 'channel' ? 'Every notification channel' : 'Every remote sensor' },
            { value: 'one', label: spec.target === 'channel' ? 'One channel' : 'One sensor' }]}
          onChange={(v) => onChange({ target: v === 'one' ? PICK : '' })} />
        {mode === 'one' && <PickOne label={spec.target === 'channel' ? 'Channel' : 'Sensor'} value={s.target} options={list}
          placeholder={spec.target === 'channel' ? 'Select a channel' : 'Select a sensor'} onChange={(v) => onChange({ target: v })} />}
      </div>
    )
  }

  if (source === 'virtualization') {
    const mode = s.target ? 'source' : s.device_id ? 'device' : s.scope_tag ? 'tag' : 'all'
    const connectors = (Array.isArray(connectorsQ.data) ? connectorsQ.data : []).map((x: any) => ({ id: String(x.id), name: x.name }))
    return (
      <div className="space-y-3">
        <ModeSelect label="Apply to" value={mode} onChange={(v) => onChange({ ...CLEARED, ...(v === 'source' ? { target: PICK } : v === 'device' ? { device_id: PICK } : v === 'tag' ? { scope_tag: PICK } : {}) })}
          options={[{ value: 'all', label: 'Every virtualisation source' }, { value: 'source', label: 'One source' },
            { value: 'device', label: 'The source linked to a device' }, { value: 'tag', label: 'Sources with a tag' }]} />
        {mode === 'source' && <PickOne label="Virtualisation source" value={s.target} options={connectors} placeholder="Select a source" onChange={(v) => onChange({ target: v })} />}
        {mode === 'device' && (
          <>
            <PickOne label="Linked device" value={s.device_id} options={deviceOptions} placeholder="Select a device" onChange={(v) => onChange({ device_id: v })} />
            <p className="text-xs text-muted">The source whose endpoint is this device’s address or host name. Its alerts show on the device.</p>
          </>
        )}
        {mode === 'tag' && <TagScope value={s.scope_tag} onChange={(t) => onChange({ scope_tag: t })} hint="Virtualisation sources carrying this tag." />}
      </div>
    )
  }

  if (source === 'netpath') {
    const probes = ((probesQ.data?.data || []) as any[]).map((p) => ({ id: String(p.name), name: p.name }))
    const mode = s.target ? 'one' : 'all'
    return (
      <div className="space-y-3">
        <ModeSelect label="Apply to" value={mode} options={[{ value: 'all', label: 'Every NetPath probe' }, { value: 'one', label: 'One probe' }]}
          onChange={(v) => onChange({ target: v === 'one' ? PICK : '' })} />
        {mode === 'one' && <PickOne label="Probe" value={s.target} options={probes} placeholder="Select a probe" onChange={(v) => onChange({ target: v })} />}
      </div>
    )
  }

  if (metric === 'agent_no_metrics') {
    const mode = s.group_id ? 'group' : s.scope_tag ? 'tag' : 'all'
    return (
      <div className="space-y-3">
        <ModeSelect label="Apply to" value={mode} onChange={(v) => onChange({ ...CLEARED, ...(v === 'group' ? { group_id: PICK } : v === 'tag' ? { scope_tag: PICK } : {}) })}
          options={[{ value: 'all', label: 'All agent servers' }, { value: 'group', label: 'Servers in a group' }, { value: 'tag', label: 'Servers with a tag' }]} />
        {mode === 'group' && <PickOne label="Device group" value={s.group_id} options={deviceGroups} placeholder="Select a group" onChange={(v) => onChange({ group_id: v })} />}
        {mode === 'tag' && <TagScope value={s.scope_tag} onChange={(t) => onChange({ scope_tag: t })} hint="Servers carrying this tag." />}
      </div>
    )
  }

  if (keys.includes('service_check_id')) {
    const mode = s.service_check_id ? 'check' : s.service_check_group_id ? 'group' : s.device_id ? 'device' : s.scope_tag ? 'tag' : 'all'
    return (
      <div className="space-y-3">
        <ModeSelect label="Apply to" value={mode}
          onChange={(v) => onChange({ ...CLEARED, ...(v === 'check' ? { service_check_id: PICK } : v === 'group' ? { service_check_group_id: PICK } : v === 'device' ? { device_id: PICK } : v === 'tag' ? { scope_tag: PICK } : {}) })}
          options={[{ value: 'all', label: 'Every service check' }, { value: 'check', label: 'One service check' },
            { value: 'group', label: 'A service check group' }, { value: 'device', label: 'Checks linked to a device' }, { value: 'tag', label: 'Checks with a tag' }]} />
        {mode === 'check' && <PickOne label="Service check" value={s.service_check_id} options={serviceChecks} placeholder="Select a check" onChange={(v) => onChange({ service_check_id: v })} />}
        {mode === 'group' && <PickOne label="Service check group" value={s.service_check_group_id} options={serviceGroups} placeholder="Select a group" onChange={(v) => onChange({ service_check_group_id: v })} />}
        {mode === 'device' && <PickOne label="Linked device" value={s.device_id} options={deviceOptions} placeholder="Select a device" onChange={(v) => onChange({ device_id: v })} />}
        {mode === 'tag' && <TagScope value={s.scope_tag} onChange={(t) => onChange({ scope_tag: t })} hint="Service checks carrying this tag." />}
      </div>
    )
  }
  return null
}

function TagScope({ value, onChange, hint }: { value: string; onChange: (t: string) => void; hint: string }) {
  const v = value === PICK ? '' : value
  return (
    <FormField label="Tag" hint={hint}>
      <TagPicker value={v ? [v] : []} onChange={(next) => onChange(next.length ? next[next.length - 1] : PICK)}
        placeholder={v ? 'Replace the tag…' : 'Choose a tag…'} />
    </FormField>
  )
}

/** Whether the draft's family scope still has a chosen-but-empty field, with what to pick. */
export function familyScopeProblem(s: Draft): string | null {
  const fields: Array<[keyof Draft, string]> = [
    ['target', 'Select the channel, sensor, source or probe, or choose every one.'],
    ['device_id', 'Select the device, or choose another scope.'],
    ['group_id', 'Select the group, or choose another scope.'],
    ['scope_tag', 'Choose a tag, or choose another scope.'],
    ['service_check_id', 'Select the service check, or choose another scope.'],
    ['service_check_group_id', 'Select the service check group, or choose another scope.'],
  ]
  for (const [k, msg] of fields) if (s[k] === PICK) return msg
  return null
}

/**
 * The state picker for a template state (enum) metric: tick the states that should alert. The rule stores
 * them as at most four AND-combined numeric conditions (template_states.conditions_for), shown here as
 * "Status is Down".
 */
export function StatePicker({ info, conditions, onChange }: {
  info: StateInfo
  conditions: Cond[]
  onChange: (conditions: Cond[]) => void
}) {
  const metric = `tpl_${info.key}`
  const chosen = statesOf(conditions, info)
  const recommended = badCodes(info)
  const toggle = (code: number) => {
    const next = chosen.includes(code) ? chosen.filter((c) => c !== code) : [...chosen, code]
    const conds = conditionsForStates(metric, info, next)
    if (conds) onChange(conds)
  }
  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-muted">Alert when the state is</div>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={`States of ${info.name} that alert`}>
        {knownCodes(info).map((code) => {
          const [text, sev] = info.labels[code]
          const on = chosen.includes(code)
          const next = on ? chosen.filter((c) => c !== code) : [...chosen, code]
          const possible = !!conditionsForStates(metric, info, next)
          return (
            <button key={code} type="button" aria-pressed={on} disabled={!possible}
              title={possible ? `Code ${code}${sev ? ` · template severity ${sev}` : ''}` : 'This combination needs more than four conditions'}
              onClick={() => toggle(code)}
              className={cn('rounded-md border px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40',
                on ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border text-muted hover:text-text')}>
              {text}{sev === 'crit' ? ' ·crit' : sev === 'warn' ? ' ·warn' : ''}
            </button>
          )
        })}
      </div>
      <p className="text-xs text-muted">
        {chosen.length ? `Alerts when ${info.groupKind === 'table' ? 'a row' : 'the device'} reports ${stateWords(info, chosen)}; one incident per ${info.groupKind === 'table' ? 'row' : 'device'}.` : 'Pick at least one state.'}
        {recommended.length > 0 && ` The template marks ${stateWords(info, recommended)} as a fault.`}
      </p>
    </div>
  )
}
