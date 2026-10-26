/**
 * NetFlow rule family in the alert wizard (CR-07): the condition (metric, band, window, hold,
 * flap suppression), the FlowFilter scope builder and a dry run against live flow data.
 */
import { useId, useMemo, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Activity, CircleHelp, Loader2, Plus, Radar, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { StatusTile } from '@/components/netflow/StatusTile'
import { FilterChips } from '@/components/netflow/FilterChips'
import { PanelMessage } from '@/components/netflow/QueryPanel'
import { apiErrorMessage, apiErrorRef, httpStatus } from '@/lib/netflow/status'
import { formatDateTime, tzOffsetLabel } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { FLOW_FILTER_LABELS, type FlowFilterKey } from '@/lib/netflow/filters'
import {
  FINDING_CONFIDENCES,
  FINDING_SEVERITIES,
  NETFLOW_METRICS,
  NETFLOW_METRIC_BY_KEY,
  UNITS,
  UNKNOWN_REASON_LABELS,
  changeMetric,
  netflowPayload,
  scopeFilter,
  validateNetflowForm,
  type ConditionMode,
  type NetflowFormErrors,
  type NetflowRuleForm,
} from './netflowAlertRule'

type Change = (next: NetflowRuleForm) => void

interface Catalog {
  applications?: string[]
  groups?: string[]
}

function useNetflowCatalog(enabled: boolean) {
  return useQuery<Catalog>({
    queryKey: ['alert-rules', 'netflow', 'catalog'],
    queryFn: async () => (await api.get('/alert-rules/netflow/catalog')).data,
    enabled,
    staleTime: 5 * 60_000,
  })
}

interface ExporterRow {
  exporter: string
  display_name?: string | null
  device_hostname?: string | null
  state?: string
}

function useExporterOptions(enabled: boolean) {
  return useQuery<ExporterRow[]>({
    queryKey: ['netflow', 'exporters', 'status', 'alert-wizard'],
    queryFn: async () => {
      const body = (await api.get('/netflow/exporters/status')).data
      const rows = body?.data?.exporters ?? body?.exporters ?? []
      return Array.isArray(rows) ? rows : []
    },
    enabled,
    staleTime: 60_000,
    retry: false,
  })
}

interface InterfaceRow {
  iface: string
  ifindex: number
  display_name?: string | null
  speed_source?: string | null
}

/** Interfaces of one exporter that carried flow traffic in the last 24 h (within visibility). */
function useExporterInterfaces(exporter: string | null, enabled: boolean) {
  return useQuery<InterfaceRow[]>({
    queryKey: ['netflow', 'interfaces', 'alert-wizard', exporter],
    queryFn: async () => {
      const body = (await api.get('/netflow/interfaces', { params: { hours: 24, exporter, envelope: 1 } })).data
      const rows = body?.data ?? body
      return Array.isArray(rows) ? rows : []
    },
    enabled: enabled && !!exporter,
    staleTime: 60_000,
    retry: false,
  })
}

/** Label + control + hint/error, associated for screen readers. */
function Field({
  label,
  hint,
  error,
  children,
  className,
  group = false,
}: {
  label: string
  hint?: ReactNode
  error?: string
  /** Receives the control id (or, for `group`, the id of the visible label) and the hint id. */
  children: (id: string, describedBy: string | undefined) => ReactNode
  className?: string
  /** The control is a radio group: label it with aria-labelledby instead of <label for>. */
  group?: boolean
}) {
  const id = useId()
  const hintId = `${id}-hint`
  const labelId = `${id}-label`
  return (
    <div className={cn('min-w-0 space-y-1.5', className)}>
      {group ? (
        <div id={labelId} className="block text-xs font-medium text-text2">{label}</div>
      ) : (
        <label htmlFor={id} className="block text-xs font-medium text-text2">
          {label}
        </label>
      )}
      {children(group ? labelId : id, hint || error ? hintId : undefined)}
      {error ? (
        <p id={hintId} className="text-xs font-medium text-danger-fg">{error}</p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-text2">{hint}</p>
      ) : null}
    </div>
  )
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  labelledBy,
  describedBy,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
  label: string
  labelledBy?: string
  describedBy?: string
}) {
  return (
    <div role="radiogroup" aria-label={labelledBy ? undefined : label} aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="flex flex-wrap gap-1 rounded-lg border border-border bg-surface2 p-1 text-xs">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'min-w-0 flex-1 rounded-md px-2.5 py-1.5 font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
            value === o.value ? 'bg-primary text-white' : 'text-text2 hover:text-text',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

const MODE_LABEL: Record<ConditionMode, string> = { above: 'Above', below: 'Below', outside: 'Outside a band' }

/* ─── Trigger step ─────────────────────────────────────────────────── */

export function NetflowConditionFields({
  form,
  onChange,
  errors,
}: {
  form: NetflowRuleForm
  onChange: Change
  errors: NetflowFormErrors
}) {
  const info = NETFLOW_METRIC_BY_KEY[form.metric]
  const set = (patch: Partial<NetflowRuleForm>) => onChange({ ...form, ...patch })
  const units = UNITS[info.unitKind]
  const showUnit = info.unitKind !== 'percent' && info.unitKind !== 'none'
  const unitSuffix = info.unitKind === 'percent' ? '%' : ''

  return (
    <div className="space-y-4">
      <fieldset>
        <legend className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-text2">What to watch</legend>
        <div role="radiogroup" aria-label="NetFlow metric" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {NETFLOW_METRICS.map((m) => {
            const active = m.metric === form.metric
            return (
              <button
                key={m.metric}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => onChange(changeMetric(form, m.metric))}
                className={cn(
                  'rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
                  active ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/40',
                )}
              >
                <div className="text-sm font-semibold text-text">{m.label}</div>
                <div className="mt-0.5 text-xs leading-snug text-text2">{m.help}</div>
              </button>
            )
          })}
        </div>
      </fieldset>

      {form.metric === 'netflow_finding' ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Finding kinds" hint="Comma-separated, e.g. syn_scan, icmp_flood. Empty = every kind." error={errors.kinds} className="sm:col-span-3">
            {(id, d) => (
              <Input id={id} aria-describedby={d} value={form.kinds} placeholder="Any kind"
                onChange={(e) => set({ kinds: e.target.value })} />
            )}
          </Field>
          <Field label="Minimum severity" error={errors.min_severity}>
            {(id, d) => (
              <select id={id} aria-describedby={d} value={form.min_severity}
                onChange={(e) => set({ min_severity: e.target.value })}
                className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                <option value="">Any severity</option>
                {FINDING_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            )}
          </Field>
          <Field label="Minimum confidence" error={errors.min_confidence}>
            {(id, d) => (
              <select id={id} aria-describedby={d} value={form.min_confidence}
                onChange={(e) => set({ min_confidence: e.target.value })}
                className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                <option value="">Any confidence</option>
                {FINDING_CONFIDENCES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            )}
          </Field>
        </div>
      ) : (
        <div className="space-y-3 rounded-lg border border-border/70 bg-surface2/30 p-3">
          {info.modes.length > 1 && (
            <Segmented
              label="Condition"
              value={form.mode}
              onChange={(mode) => set({ mode })}
              options={info.modes.map((m) => ({ value: m, label: MODE_LABEL[m] }))}
            />
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_auto]">
            <div className="grid grid-cols-2 gap-2">
              <Field
                label={form.mode === 'outside' ? 'Lower bound' : form.metric === 'netflow_exporter_silent' ? 'Silent for more than' : 'Threshold'}
                error={errors.threshold}
              >
                {(id, d) => (
                  <div className="flex items-center gap-1.5">
                    <Input id={id} aria-describedby={d} inputMode="decimal" value={form.threshold}
                      onChange={(e) => set({ threshold: e.target.value })} />
                    {unitSuffix && <span className="text-sm text-text2">{unitSuffix}</span>}
                  </div>
                )}
              </Field>
              {form.mode === 'outside' ? (
                <Field label="Upper bound" error={errors.bandHigh}>
                  {(id, d) => (
                    <div className="flex items-center gap-1.5">
                      <Input id={id} aria-describedby={d} inputMode="decimal" value={form.bandHigh}
                        onChange={(e) => set({ bandHigh: e.target.value })} />
                      {unitSuffix && <span className="text-sm text-text2">{unitSuffix}</span>}
                    </div>
                  )}
                </Field>
              ) : showUnit ? (
                <Field label="Unit">
                  {(id) => (
                    <select id={id} value={form.unit} onChange={(e) => set({ unit: e.target.value })}
                      className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                      {units.map(([u]) => <option key={u} value={u}>{u}</option>)}
                    </select>
                  )}
                </Field>
              ) : <div />}
            </div>
            {form.mode === 'outside' && showUnit && (
              <Field label="Unit">
                {(id) => (
                  <select id={id} value={form.unit} onChange={(e) => set({ unit: e.target.value })}
                    className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text sm:w-24">
                    {units.map(([u]) => <option key={u} value={u}>{u}</option>)}
                  </select>
                )}
              </Field>
            )}
          </div>
          {form.mode !== 'outside' && form.metric !== 'netflow_exporter_silent' && (
            <label className="flex items-center gap-2 text-xs text-text2">
              <input type="checkbox" checked={form.inclusive} onChange={(e) => set({ inclusive: e.target.checked })}
                className="h-4 w-4 rounded border-border" />
              Include the threshold itself ({form.mode === 'below' ? '≤' : '≥'} instead of {form.mode === 'below' ? '<' : '>'})
            </label>
          )}
          {info.windowRange && (
            <Field
              label="Measured over"
              hint={form.metric === 'netflow_interface_util_pct'
                ? 'Minutes of complete 5-minute slots, ending at the last closed slot.'
                : 'Minutes, ending at the last closed minute (a short settle delay lets late flow records arrive).'}
              error={errors.window_min}
            >
              {(id, d) => (
                <div className="flex items-center gap-2">
                  <Input id={id} aria-describedby={d} className="w-24" inputMode="numeric" value={form.window_min}
                    onChange={(e) => set({ window_min: e.target.value })} />
                  <span className="text-sm text-text2">minutes</span>
                </div>
              )}
            </Field>
          )}
          {form.metric === 'netflow_traffic_bps' && (
            <Field group label="Evaluate" hint="Each exporter separately gives one alert per exporter, attached to its device.">
              {(lid, d) => (
                <Segmented label="Evaluate" labelledBy={lid} describedBy={d} value={form.split} onChange={(split) => set({ split })}
                  options={[{ value: 'none', label: 'Whole scope' }, { value: 'exporter', label: 'Each exporter' }]} />
              )}
            </Field>
          )}
          {form.metric === 'netflow_endpoint_bytes' && (
            <Field group label="Count a host's traffic">
              {(lid) => (
                <Segmented label="Host side" labelledBy={lid} value={form.side} onChange={(side) => set({ side })}
                  options={[{ value: 'src', label: 'Sent' }, { value: 'dst', label: 'Received' }, { value: 'any', label: 'Sent + received' }]} />
              )}
            </Field>
          )}
          {form.metric === 'netflow_interface_util_pct' && (
            <Field group label="Direction" hint="In = received on the interface, out = transmitted. Each direction is its own alert.">
              {(lid, d) => (
                <Segmented label="Direction" labelledBy={lid} describedBy={d} value={form.direction} onChange={(direction) => set({ direction })}
                  options={[{ value: 'in', label: 'In' }, { value: 'out', label: 'Out' }, { value: 'either', label: 'Either' }]} />
              )}
            </Field>
          )}
        </div>
      )}

      <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <legend className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-text2">Noise control</legend>
        <Field label="Raise after" hint="Consecutive breaching evaluations (one per minute; per 5-minute slot for interfaces)." error={errors.hold}>
          {(id, d) => (
            <div className="flex items-center gap-2">
              <Input id={id} aria-describedby={d} className="w-20" inputMode="numeric" value={form.hold}
                onChange={(e) => set({ hold: e.target.value })} />
              <span className="text-sm text-text2">evaluations</span>
            </div>
          )}
        </Field>
        <Field label="Resolve after" hint="Consecutive clear evaluations. Missing data never counts as clear." error={errors.clear_hold}>
          {(id, d) => (
            <div className="flex items-center gap-2">
              <Input id={id} aria-describedby={d} className="w-20" inputMode="numeric" value={form.clear_hold}
                onChange={(e) => set({ clear_hold: e.target.value })} />
              <span className="text-sm text-text2">evaluations</span>
            </div>
          )}
        </Field>
        <Field label="Flap suppression" hint="Keep the alert open once it has changed state this often (0 = off)." error={errors.flap_max}>
          {(id, d) => (
            <div className="flex items-center gap-2">
              <Input id={id} aria-describedby={d} className="w-20" inputMode="numeric" value={form.flap_max}
                onChange={(e) => set({ flap_max: e.target.value })} />
              <span className="text-sm text-text2">changes</span>
            </div>
          )}
        </Field>
        <Field label="Within" error={errors.flap_window_min}>
          {(id, d) => (
            <div className="flex items-center gap-2">
              <Input id={id} aria-describedby={d} className="w-24" inputMode="numeric" value={form.flap_window_min}
                onChange={(e) => set({ flap_window_min: e.target.value })} />
              <span className="text-sm text-text2">minutes</span>
            </div>
          )}
        </Field>
      </fieldset>
    </div>
  )
}

/* ─── Scope step ───────────────────────────────────────────────────── */

const PLACEHOLDER: Partial<Record<FlowFilterKey, string>> = {
  talker: '10.0.0.5 or 10.0.0.0/24',
  src: '10.0.0.0/8',
  dst: '203.0.113.10',
  not_talker: '10.9.9.9',
  iface: '3@10.0.0.1',
  protocol: '6 (TCP), 17 (UDP)…',
  port: '443',
  dst_port: '53',
  dst_ports: '80,443',
  service_port: '443',
  dscp: '46',
  asn: '15169',
  hour: '0–23',
  dow: '1 (Mon) – 7 (Sun)',
  min_bytes: 'bytes per record',
  max_bytes: 'bytes per record',
}

const ENUM_OPTIONS: Partial<Record<FlowFilterKey, { value: string; label: string }[]>> = {
  iface_dir: [{ value: 'in', label: 'Inbound only' }, { value: 'out', label: 'Outbound only' }, { value: 'any', label: 'Either' }],
  ip_version: [{ value: '4', label: 'IPv4' }, { value: '6', label: 'IPv6' }],
  direction: [{ value: 'ingress', label: 'Ingress-monitored' }, { value: 'egress', label: 'Egress-monitored' }],
  count: [{ value: 'all', label: 'Every observation' }, { value: 'edge', label: 'Edge exporters only' }],
  tcpflag: ['syn_only', 'ack_only', 'rst', 'fin', 'psh', 'urg', 'no_flags'].map((v) => ({ value: v, label: v })),
}

export function NetflowScopeFields({
  form,
  onChange,
  errors,
  open,
}: {
  form: NetflowRuleForm
  onChange: Change
  errors: NetflowFormErrors
  open: boolean
}) {
  const info = NETFLOW_METRIC_BY_KEY[form.metric]
  const catalog = useNetflowCatalog(open)
  const exporters = useExporterOptions(open)
  const keys = (Object.keys(form.scope) as FlowFilterKey[]).filter((k) => info.scopeKeys.includes(k))
  const addable = info.scopeKeys.filter((k) => !(k in form.scope) && k !== 'tz')
  const setKey = (k: FlowFilterKey, v: string) => onChange({ ...form, scope: { ...form.scope, [k]: v } })
  const removeKey = (k: FlowFilterKey) => {
    const next = { ...form.scope }
    delete next[k]
    if (k === 'iface') delete next.iface_dir
    onChange({ ...form, scope: next })
  }
  const listId = useId()
  const filter = useMemo(() => scopeFilter(form), [form])
  const exporterRows = exporters.data ?? []
  const pinnedExporter = filter.exporter ?? null
  const interfaces = useExporterInterfaces(pinnedExporter, open && 'iface' in form.scope)
  const ifaceRows = interfaces.data ?? []

  return (
    <div className="space-y-3">
      <p className="text-xs text-text2">
        The scope is a NetFlow filter — the same one the NetFlow pages use. With no filter the rule watches{' '}
        {info.metric === 'netflow_exporter_silent' ? 'every exporter' : 'all flow traffic'} you are allowed to see.
        Evaluated as {info.subject}.
      </p>
      {keys.length === 0 && (
        <div className="rounded-lg border border-dashed border-border px-3 py-3 text-xs text-text2">No filter: whole visible scope.</div>
      )}
      <ul className="space-y-2" aria-label="Scope filters">
        {keys.map((k) => {
          const value = form.scope[k] ?? ''
          const label = FLOW_FILTER_LABELS[k]
          return (
            <li key={k} className="flex items-start gap-2 rounded-lg border border-border/70 bg-surface2/30 p-2.5">
              <Field label={label} error={errors[k]} className="flex-1">
                {(id, d) =>
                  k === 'exporter' && exporterRows.length > 0 ? (
                    <select id={id} aria-describedby={d} value={value} onChange={(e) => setKey(k, e.target.value)}
                      className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                      <option value="">Pick an exporter…</option>
                      {value && !exporterRows.some((r) => r.exporter === value) && <option value={value}>{value}</option>}
                      {exporterRows.map((r) => (
                        <option key={r.exporter} value={r.exporter}>
                          {(r.display_name || r.device_hostname) ? `${r.display_name || r.device_hostname} (${r.exporter})` : r.exporter}
                        </option>
                      ))}
                    </select>
                  ) : k === 'iface' && pinnedExporter && ifaceRows.length > 0 ? (
                    <select id={id} aria-describedby={d} value={value} onChange={(e) => setKey(k, e.target.value)}
                      className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                      <option value="">Pick an interface…</option>
                      {value && !ifaceRows.some((r) => r.iface === value) && <option value={value}>{value}</option>}
                      {ifaceRows.map((r) => (
                        <option key={r.iface} value={r.iface}>
                          {`${r.display_name || `ifIndex ${r.ifindex}`} (ifIndex ${r.ifindex})${
                            form.metric === 'netflow_interface_util_pct' && r.speed_source === 'unknown' ? ' — speed unknown' : ''}`}
                        </option>
                      ))}
                    </select>
                  ) : ENUM_OPTIONS[k] ? (
                    <select id={id} aria-describedby={d} value={value} onChange={(e) => setKey(k, e.target.value)}
                      className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text">
                      <option value="">Choose…</option>
                      {ENUM_OPTIONS[k]!.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : (
                    <>
                      <Input
                        id={id}
                        aria-describedby={d}
                        value={value}
                        placeholder={k === 'iface' && pinnedExporter ? `3@${pinnedExporter}` : PLACEHOLDER[k] ?? ''}
                        list={k === 'app' || k === 'group' ? `${listId}-${k}` : undefined}
                        onChange={(e) => setKey(k, e.target.value)}
                        className="font-mono"
                      />
                      {k === 'app' && (
                        <datalist id={`${listId}-app`}>
                          {(catalog.data?.applications ?? []).map((a) => <option key={a} value={a} />)}
                        </datalist>
                      )}
                      {k === 'group' && (
                        <datalist id={`${listId}-group`}>
                          {(catalog.data?.groups ?? []).map((g) => <option key={g} value={g} />)}
                        </datalist>
                      )}
                    </>
                  )
                }
              </Field>
              <Button type="button" variant="ghost" size="icon" className="mt-6 h-9 w-9 shrink-0 text-text2 hover:text-danger-fg"
                aria-label={`Remove ${label} filter`} onClick={() => removeKey(k)}>
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
            </li>
          )
        })}
      </ul>
      {errors.scope && <p className="text-xs font-medium text-danger-fg">{errors.scope}</p>}
      {addable.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor={`${listId}-add`}>Add a scope filter</label>
          <select
            id={`${listId}-add`}
            value=""
            onChange={(e) => {
              const k = e.target.value as FlowFilterKey
              if (k) setKey(k, '')
            }}
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm text-text"
          >
            <option value="">+ Add a filter…</option>
            {addable.map((k) => <option key={k} value={k}>{FLOW_FILTER_LABELS[k]}</option>)}
          </select>
          {exporters.isError && (
            <span className="text-xs text-text2">Exporter list unavailable — type the exporter address.</span>
          )}
        </div>
      )}
      {Object.keys(filter).length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-text2">The rule will match</div>
          <FilterChips filter={filter} />
        </div>
      )}
    </div>
  )
}

/* ─── Dry run ──────────────────────────────────────────────────────── */

interface DryReading {
  subject: string
  label: string
  value: number | null
  reading: string
  breach: boolean | null
  reason: string | null
}

interface DryRun {
  status: 'ok' | 'no_data' | 'failed' | 'skipped'
  reason: string | null
  error_id: string | null
  window: { from: string | null; to: string | null }
  condition: string
  scope_label: string
  truncated: boolean
  readings: DryReading[]
  breaching: number
  unknown: number
}

export function NetflowDryRun({ form }: { form: NetflowRuleForm }) {
  const errors = validateNetflowForm(form)
  const valid = Object.keys(errors).length === 0
  const run = useMutation<DryRun>({
    mutationFn: async () => (await api.post('/alert-rules/netflow/evaluate', netflowPayload(form), { timeout: 60_000 })).data,
  })
  const tz = displayTimeZone()
  const data = run.data
  return (
    <section aria-labelledby="nf-dry-run" className="space-y-3 rounded-lg border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h4 id="nf-dry-run" className="flex items-center gap-1.5 text-sm font-semibold text-text">
            <Radar className="h-4 w-4 text-primary" aria-hidden /> Test against live flow data
          </h4>
          <p className="text-xs text-text2">What the evaluator would read right now, within your NetFlow visibility. Nothing is saved.</p>
        </div>
        <Button type="button" variant="outline" size="sm" disabled={!valid || run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Activity className="h-3.5 w-3.5" aria-hidden />}
          {run.isPending ? 'Evaluating…' : 'Run test'}
        </Button>
      </div>
      {!valid && <p className="text-xs text-text2">Fix the highlighted fields to run a test.</p>}
      {run.isError && (
        httpStatus(run.error) === 403 ? (
          <PanelMessage icon={CircleHelp} title="No NetFlow access" detail="Your role does not include netflow.view, so live readings cannot be shown." />
        ) : (
          <PanelMessage icon={CircleHelp} iconClass="text-danger-fg" title="The test could not run"
            detail={apiErrorMessage(run.error)} errorRef={apiErrorRef(run.error)} onRetry={() => run.mutate()} />
        )
      )}
      {data && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <StatusTile tone={data.status === 'failed' ? 'danger' : data.breaching ? 'warning' : data.status === 'no_data' ? 'unknown' : 'success'}
              label="Would alert now"
              value={data.status === 'failed' ? '—' : `${data.breaching} of ${data.readings.length}`}
              word={data.status === 'failed' ? 'Failed' : data.breaching ? 'Breaching' : data.status === 'no_data' ? 'No data' : 'Clear'}
              caption={data.condition} />
            <StatusTile tone={data.unknown ? 'unknown' : 'neutral'} label="Unknown readings" value={String(data.unknown)}
              word={data.unknown ? 'Unknown' : null} caption="never treated as zero" />
            <StatusTile tone="neutral" label="Window read" word={null}
              value={data.window.from && data.window.to
                ? `${formatDateTime(data.window.from, tz, { zone: 'none', timeOnly: true, seconds: false })}–${formatDateTime(data.window.to, tz, { zone: 'none', timeOnly: true, seconds: false })}`
                : data.window.to ? formatDateTime(data.window.to, tz, { zone: 'none', timeOnly: true, seconds: false }) : '—'}
              caption={data.window.to ? `${tzOffsetLabel(tz, data.window.to)}, ending at the last closed slot` : 'no window'} />
          </div>
          {data.status === 'failed' ? (
            <p className="text-xs text-danger-fg">Evaluation failed{data.error_id ? ` (reference ${data.error_id})` : ''}.</p>
          ) : data.readings.length === 0 ? (
            <p className="text-xs text-text2">Nothing in scope delivered flow data in this window.</p>
          ) : (
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[20rem] text-left text-xs">
                <caption className="sr-only">Live readings per subject</caption>
                <thead className="bg-surface2 text-text2">
                  <tr>
                    <th scope="col" className="px-2.5 py-1.5 font-medium">Subject</th>
                    <th scope="col" className="px-2.5 py-1.5 text-right font-medium">Reading</th>
                    <th scope="col" className="px-2.5 py-1.5 font-medium">State</th>
                  </tr>
                </thead>
                <tbody>
                  {data.readings.slice(0, 25).map((r) => (
                    <tr key={r.subject} className="border-t border-border/60">
                      <td className="min-w-[9rem] break-words px-2.5 py-1.5 text-text">{r.label}</td>
                      <td className="whitespace-nowrap px-2.5 py-1.5 text-right tabular-nums text-text">{r.value === null ? '—' : r.reading}</td>
                      <td className="px-2.5 py-1.5">
                        {r.breach === null ? (
                          <span className="text-text2">Unknown — {UNKNOWN_REASON_LABELS[r.reason ?? ''] ?? r.reason ?? 'no reading'}</span>
                        ) : r.breach ? (
                          <span className="font-semibold text-warning-fg">Breaching</span>
                        ) : (
                          <span className="text-success-fg">Clear</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(data.truncated || data.readings.length > 25) && (
            <p className="text-xs text-text2">Showing the first 25 subjects{data.truncated ? '; more hosts are above the threshold than one evaluation raises' : ''}.</p>
          )}
        </div>
      )}
    </section>
  )
}

export { validateNetflowForm }

/** "Add a filter" affordance used by the wizard's empty state. */
export function AddScopeHint() {
  return (
    <p className="flex items-center gap-1 text-xs text-text2">
      <Plus className="h-3 w-3" aria-hidden /> Narrow the scope with exporter, interface, IP group, application, port or host filters.
    </p>
  )
}
