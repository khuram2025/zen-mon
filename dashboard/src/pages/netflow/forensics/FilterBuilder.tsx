import { useId, type ReactNode } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  FLOW_FILTER_LABELS,
  FW_EVENT_LABELS,
  FW_EVENTS,
  TCP_FLAG_FILTERS,
  TCP_FLAG_LABELS,
  type FlowFilterErrors,
  type FlowFilterKey,
} from '@/lib/netflow/filters'
import { dowName, protocolLabel } from '@/lib/netflow/format'
import { NET_CLASS_OPTIONS, normalizeProtocolText, PROTOCOL_SUGGESTIONS, type DraftState } from './model'

type Option = { value: string; label: string }

interface FieldSpec {
  key: FlowFilterKey
  label?: string
  placeholder?: string
  hint?: string
  numeric?: boolean
  options?: Option[]
  /** Wider cell (two columns on wide screens). */
  wide?: boolean
}

const ANY = { value: '', label: 'Any' }

const PRIMARY: FieldSpec[] = [
  { key: 'talker', label: 'Host (source or destination)', placeholder: '10.0.0.5, 10.0.0.0/8 or 2001:db8::/32' },
  { key: 'src', label: 'Source', placeholder: 'IP or CIDR' },
  { key: 'dst', label: 'Destination', placeholder: 'IP or CIDR' },
  { key: 'protocol', label: 'Protocol', placeholder: 'tcp, udp, 6, 89…', hint: 'Name or number 0–255' },
  { key: 'port', label: 'Port (either side)', placeholder: '443', numeric: true },
  { key: 'exporter', label: 'Exporter', placeholder: 'Exporter IP address' },
]

const GROUPS: { title: string; fields: FieldSpec[] }[] = [
  {
    title: 'Exclusions',
    fields: [
      { key: 'not_talker', label: 'Not host', placeholder: 'IP or CIDR', hint: 'Excluded as source and destination' },
      { key: 'not_src', label: 'Not source', placeholder: 'IP or CIDR' },
      { key: 'not_dst', label: 'Not destination', placeholder: 'IP or CIDR' },
    ],
  },
  {
    title: 'Ports and services',
    fields: [
      { key: 'src_port', placeholder: '0–65535', numeric: true },
      { key: 'dst_port', placeholder: '0–65535', numeric: true },
      { key: 'dst_ports', placeholder: '22,3389', hint: 'Up to 32 ports, comma-separated' },
      { key: 'service_port', placeholder: '443', numeric: true, hint: 'Both directions of the service' },
      { key: 'app', label: 'Application', placeholder: 'e.g. Web (HTTP/HTTPS)', hint: 'Exact application name' },
    ],
  },
  {
    title: 'Addresses and routing',
    fields: [
      { key: 'ip_version', options: [ANY, { value: '4', label: 'IPv4' }, { value: '6', label: 'IPv6' }] },
      { key: 'netclass', options: [ANY, ...NET_CLASS_OPTIONS.map((n) => ({ value: n, label: n }))], hint: 'Source or destination in the class' },
      { key: 'group', label: 'IP group', placeholder: 'Group name', hint: 'Source or destination in the group' },
      { key: 'asn', label: 'AS number', placeholder: '64512', numeric: true, hint: 'Source or destination AS' },
    ],
  },
  {
    title: 'Interfaces and direction',
    fields: [
      { key: 'iface', label: 'Interface (ifIndex)', placeholder: '3 or 3@192.0.2.1', hint: 'Qualify with @exporter for one device' },
      { key: 'iface_dir', label: 'Interface side', options: [{ value: '', label: 'Input or output' }, { value: 'in', label: 'Input only' }, { value: 'out', label: 'Output only' }] },
      { key: 'direction', label: 'Flow direction', options: [ANY, { value: 'ingress', label: 'Ingress-monitored' }, { value: 'egress', label: 'Egress-monitored' }], hint: 'As exported (IE 61)' },
    ],
  },
  {
    title: 'QoS, flags and size',
    fields: [
      { key: 'dscp', placeholder: '0–63 (46 = EF)', numeric: true },
      { key: 'tcpflag', label: 'TCP flags', options: [ANY, ...TCP_FLAG_FILTERS.map((f) => ({ value: f, label: TCP_FLAG_LABELS[f] }))], hint: 'Only records whose flags were exported' },
      { key: 'min_bytes', placeholder: 'bytes', numeric: true },
      { key: 'max_bytes', placeholder: 'bytes', numeric: true },
    ],
  },
  {
    title: 'Firewall, time of day and counting',
    fields: [
      { key: 'fw_event', label: 'Firewall event', options: [ANY, ...FW_EVENTS.map((e) => ({ value: e, label: FW_EVENT_LABELS[e] }))], hint: 'NSEL / IPFIX firewall events' },
      { key: 'hour', label: 'Hour of day', options: [ANY, ...Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: `${String(h).padStart(2, '0')}:00–${String(h).padStart(2, '0')}:59` }))] },
      { key: 'dow', label: 'Day of week', options: [ANY, ...Array.from({ length: 7 }, (_, i) => ({ value: String(i + 1), label: dowName(i + 1) }))] },
      { key: 'count', label: 'Counting', options: [{ value: '', label: 'All observations' }, { value: 'edge', label: 'Edge exporters only' }], hint: 'Each flow once at its edge collection point' },
    ],
  },
]

export const ADVANCED_KEYS: FlowFilterKey[] = GROUPS.flatMap((g) => g.fields.map((f) => f.key))

export interface FilterBuilderProps {
  draft: DraftState
  onChange: (key: FlowFilterKey, value: string) => void
  errors: FlowFilterErrors
  showErrors: boolean
  advancedOpen: boolean
  onToggleAdvanced: () => void
  /** Zone hour/day filters are evaluated in. */
  tz: string
}

/** Every retained flow field as a draft filter (FE-04); nothing runs until the form is submitted. */
export function FilterBuilder({ draft, onChange, errors, showErrors, advancedOpen, onToggleAdvanced, tz }: FilterBuilderProps) {
  const uid = useId()
  const advancedCount = ADVANCED_KEYS.filter((k) => (draft[k] ?? '').trim() !== '').length
  const advancedErrors = ADVANCED_KEYS.filter((k) => showErrors && errors[k]).length
  const panelId = `${uid}-advanced`
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {PRIMARY.map((f) => (
          <Field key={f.key} spec={f} uid={uid} value={draft[f.key] ?? ''} onChange={onChange} error={showErrors ? errors[f.key] : undefined} tz={tz} />
        ))}
      </div>
      <button
        type="button"
        onClick={onToggleAdvanced}
        aria-expanded={advancedOpen}
        aria-controls={panelId}
        className="inline-flex items-center gap-1 rounded-md px-1 py-0.5 text-xs font-medium text-text2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        {advancedOpen ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
        All filters
        {advancedCount > 0 && <span className="rounded-full bg-primary/15 px-1.5 text-2xs font-semibold text-text">{advancedCount} set</span>}
        {advancedErrors > 0 && <span className="rounded-full bg-danger/15 px-1.5 text-2xs font-semibold text-danger-fg">{advancedErrors} to fix</span>}
      </button>
      {advancedOpen && (
        <div id={panelId} className="space-y-4 rounded-md border border-border bg-surface2/30 p-3">
          {GROUPS.map((g) => (
            <fieldset key={g.title} className="space-y-2">
              <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-text2">{g.title}</legend>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                {g.fields.map((f) => (
                  <Field key={f.key} spec={f} uid={uid} value={draft[f.key] ?? ''} onChange={onChange} error={showErrors ? errors[f.key] : undefined} tz={tz} />
                ))}
              </div>
            </fieldset>
          ))}
          <p className="text-xs text-text2">
            All conditions must match (AND). Exclusions remove matching records. Hour and day are evaluated in {tz}.
          </p>
        </div>
      )}
      <datalist id={`${uid}-protocols`}>
        {PROTOCOL_SUGGESTIONS.map((p) => (
          <option key={p.value} value={String(p.value)}>{p.label}</option>
        ))}
      </datalist>
    </div>
  )
}

function Field({
  spec,
  uid,
  value,
  onChange,
  error,
  tz,
}: {
  spec: FieldSpec
  uid: string
  value: string
  onChange: (key: FlowFilterKey, value: string) => void
  error?: string
  tz: string
}) {
  const id = `${uid}-${spec.key}`
  const errId = `${id}-err`
  const hintId = `${id}-hint`
  const label = spec.label ?? FLOW_FILTER_LABELS[spec.key]
  let hint: ReactNode = spec.hint
  if (spec.key === 'protocol' && value.trim()) {
    const n = Number(normalizeProtocolText(value))
    if (Number.isInteger(n) && n >= 0 && n <= 255) hint = protocolLabel(n)
  }
  if ((spec.key === 'hour' || spec.key === 'dow') && value) hint = `Evaluated in ${tz}`
  const describedBy = error ? errId : hint ? hintId : undefined
  const base = cn(
    'mt-1 h-9 w-full rounded-md border bg-surface px-2.5 text-sm text-text shadow-sm placeholder:text-muted',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
    error ? 'border-danger focus-visible:border-danger' : 'border-border focus-visible:border-primary',
  )
  let control: ReactNode
  if (spec.options) {
    const known = spec.options.some((o) => o.value === value)
    control = (
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(spec.key, e.target.value)}
        aria-invalid={!!error}
        aria-describedby={describedBy}
        className={base}
      >
        {!known && value !== '' && <option value={value}>{`“${value}” (not valid)`}</option>}
        {spec.options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    )
  } else {
    control = (
      <input
        id={id}
        value={value}
        onChange={(e) => onChange(spec.key, e.target.value)}
        placeholder={spec.placeholder}
        inputMode={spec.numeric ? 'numeric' : undefined}
        list={spec.key === 'protocol' ? `${uid}-protocols` : undefined}
        autoComplete="off"
        spellCheck={false}
        aria-invalid={!!error}
        aria-describedby={describedBy}
        className={cn(base, 'font-mono text-sm')}
      />
    )
  }
  return (
    <div className={cn('min-w-0', spec.wide && 'sm:col-span-2')}>
      <label htmlFor={id} className="text-xs font-medium text-text2">{label}</label>
      {control}
      {error ? (
        <p id={errId} className="mt-1 text-xs font-medium text-danger-fg">{error}</p>
      ) : hint ? (
        <p id={hintId} className="mt-1 text-xs text-text2">{hint}</p>
      ) : null}
    </div>
  )
}
