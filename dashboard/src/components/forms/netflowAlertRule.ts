/**
 * NetFlow alert rules in the rule wizard (CR-07): the form model, unit conversion, validation and
 * the payload the API stores (`metric`, `operator`, `threshold`, `flow_scope`, `flow_params`).
 *
 * The scope is a FlowFilter in the canonical keys of `lib/netflow/filters` (the same model the
 * NetFlow pages use), so a rule's scope reads — and links — exactly like a NetFlow view.
 * Thresholds are edited in human units (Mbps, GB, minutes) and stored in base units (bit/s,
 * bytes, seconds); zero is a value, never "unset".
 */
import {
  FLOW_FILTER_KEYS,
  FLOW_FILTER_LABELS,
  flowFilterFromDraft,
  parseIfaceValue,
  type FlowFilter,
  type FlowFilterDraft,
  type FlowFilterKey,
} from '@/lib/netflow/filters'

export type NetflowMetric =
  | 'netflow_traffic_bps'
  | 'netflow_endpoint_bytes'
  | 'netflow_exporter_silent'
  | 'netflow_interface_util_pct'
  | 'netflow_finding'

export type ConditionMode = 'above' | 'below' | 'outside'
export type UnitKind = 'bps' | 'bytes' | 'seconds' | 'percent' | 'none'

export interface NetflowMetricInfo {
  metric: NetflowMetric
  label: string
  short: string
  unitKind: UnitKind
  modes: ConditionMode[]
  /** FlowFilter keys this metric may be scoped by (server: _SCOPE_KEYS_BY_METRIC). */
  scopeKeys: FlowFilterKey[]
  /** [min, max] evaluation window in minutes, or null when the metric has none. */
  windowRange: [number, number] | null
  windowStep: number
  defaults: { window_min?: number; hold: number; clear_hold: number; flap_max: number; flap_window_min: number }
  subject: string
  help: string
}

const TRAFFIC_KEYS = FLOW_FILTER_KEYS.filter((k) => k !== 'fw_event') as FlowFilterKey[]

export const NETFLOW_METRICS: NetflowMetricInfo[] = [
  {
    metric: 'netflow_traffic_bps',
    label: 'Flow traffic rate',
    short: 'Traffic rate',
    unitKind: 'bps',
    modes: ['above', 'below', 'outside'],
    scopeKeys: TRAFFIC_KEYS,
    windowRange: [1, 60],
    windowStep: 1,
    defaults: { window_min: 5, hold: 2, clear_hold: 2, flap_max: 4, flap_window_min: 60 },
    subject: 'one reading for the whole scope, or one per exporter',
    help: 'Average bit rate of the traffic matching the scope over the last closed minutes.',
  },
  {
    metric: 'netflow_endpoint_bytes',
    label: 'Host transfer volume',
    short: 'Host volume',
    unitKind: 'bytes',
    modes: ['above'],
    scopeKeys: TRAFFIC_KEYS,
    windowRange: [1, 1440],
    windowStep: 1,
    defaults: { window_min: 15, hold: 1, clear_hold: 2, flap_max: 4, flap_window_min: 60 },
    subject: 'one reading per host',
    help: 'One alert per host that sends, receives or moves more than the threshold in the window.',
  },
  {
    metric: 'netflow_exporter_silent',
    label: 'Flow export silence',
    short: 'Exporter silent',
    unitKind: 'seconds',
    modes: ['above'],
    scopeKeys: ['exporter'],
    windowRange: null,
    windowStep: 1,
    defaults: { hold: 1, clear_hold: 1, flap_max: 4, flap_window_min: 60 },
    subject: 'one reading per exporter',
    help: 'Time since an exporter last delivered flow records to this appliance. This is flow-export ' +
      'silence, not device reachability.',
  },
  {
    metric: 'netflow_interface_util_pct',
    label: 'Interface utilisation (flow)',
    short: 'Interface utilisation',
    unitKind: 'percent',
    modes: ['above', 'below', 'outside'],
    scopeKeys: ['exporter', 'iface', 'iface_dir'],
    windowRange: [5, 60],
    windowStep: 5,
    defaults: { window_min: 15, hold: 2, clear_hold: 2, flap_max: 4, flap_window_min: 60 },
    subject: 'one reading per interface and direction',
    help: 'Per-direction utilisation from flow data against the configured, override or SNMP speed. ' +
      'An interface with no known speed is not evaluated.',
  },
  {
    metric: 'netflow_finding',
    label: 'NetFlow detection finding',
    short: 'Detection finding',
    unitKind: 'none',
    modes: ['above'],
    scopeKeys: ['exporter', 'talker'],
    windowRange: null,
    windowStep: 1,
    defaults: { hold: 1, clear_hold: 1, flap_max: 0, flap_window_min: 60 },
    subject: 'one alert per matching finding',
    help: 'Raises a platform alert for each open detection finding that matches; it resolves when the ' +
      'finding is resolved or suppressed.',
  },
]

export const NETFLOW_METRIC_BY_KEY: Record<NetflowMetric, NetflowMetricInfo> = Object.fromEntries(
  NETFLOW_METRICS.map((m) => [m.metric, m]),
) as Record<NetflowMetric, NetflowMetricInfo>

export function isNetflowMetric(metric: string | null | undefined): metric is NetflowMetric {
  return !!metric && metric in NETFLOW_METRIC_BY_KEY
}

/* ─── Units ──────────────────────────────────────────────────────────── */

export const UNITS: Record<UnitKind, [string, number][]> = {
  bps: [['bps', 1], ['kbps', 1e3], ['Mbps', 1e6], ['Gbps', 1e9]],
  bytes: [['B', 1], ['kB', 1e3], ['MB', 1e6], ['GB', 1e9], ['TB', 1e12]],
  seconds: [['s', 1], ['min', 60], ['h', 3600]],
  percent: [['%', 1]],
  none: [['', 1]],
}

export function unitFactor(kind: UnitKind, unit: string): number {
  return UNITS[kind].find(([u]) => u === unit)?.[1] ?? 1
}

/** The largest unit in which `base` is at least 1 (1.5e9 bps → 1.5 Gbps). */
export function bestUnit(kind: UnitKind, base: number): string {
  const units = UNITS[kind]
  let pick = units[0][0]
  for (const [u, f] of units) if (Math.abs(base) >= f) pick = u
  return pick
}

export function toBase(kind: UnitKind, text: string, unit: string): number | null {
  if (text.trim() === '') return null
  const n = Number(text)
  if (!Number.isFinite(n)) return null
  return n * unitFactor(kind, unit)
}

export function fromBase(kind: UnitKind, base: number, unit: string): string {
  const v = base / unitFactor(kind, unit)
  return String(Number(v.toPrecision(12)))
}

/** "1.5 Gbps", "10 min", "80%" — for summaries. */
export function formatThreshold(kind: UnitKind, base: number | null): string {
  if (base === null) return '—'
  if (kind === 'none') return ''
  const unit = kind === 'percent' ? '%' : bestUnit(kind, base)
  const v = fromBase(kind, base, unit)
  return kind === 'percent' ? `${v}%` : `${v} ${unit}`
}

/* ─── Form ───────────────────────────────────────────────────────────── */

export const FINDING_SEVERITIES = ['info', 'low', 'medium', 'warning', 'high', 'critical'] as const
export const FINDING_CONFIDENCES = ['low', 'medium', 'high'] as const

export interface NetflowRuleForm {
  metric: NetflowMetric
  mode: ConditionMode
  /** `>=` / `<=` instead of `>` / `<`. */
  inclusive: boolean
  threshold: string
  bandHigh: string
  unit: string
  window_min: string
  hold: string
  clear_hold: string
  flap_max: string
  flap_window_min: string
  split: 'none' | 'exporter'
  side: 'src' | 'dst' | 'any'
  direction: 'in' | 'out' | 'either'
  kinds: string
  min_severity: string
  min_confidence: string
  scope: FlowFilterDraft
  /** Stored parameters this editor does not show (settle delay, caps, look-back): kept on save. */
  extra?: Record<string, unknown>
}

/** Advanced parameters set through the API; the editor preserves them unchanged. */
export const PRESERVED_PARAMS = ['settle_s', 'max_subjects', 'lookback_h', 'max_age_h'] as const

const DEFAULT_UNIT: Record<UnitKind, string> = { bps: 'Mbps', bytes: 'GB', seconds: 'min', percent: '%', none: '' }
const DEFAULT_THRESHOLD: Record<NetflowMetric, string> = {
  netflow_traffic_bps: '100',
  netflow_endpoint_bytes: '10',
  netflow_exporter_silent: '10',
  netflow_interface_util_pct: '80',
  netflow_finding: '1',
}

export function defaultNetflowForm(metric: NetflowMetric = 'netflow_traffic_bps', scope: FlowFilterDraft = {}): NetflowRuleForm {
  const info = NETFLOW_METRIC_BY_KEY[metric]
  const d = info.defaults
  return {
    metric,
    mode: 'above',
    inclusive: false,
    threshold: DEFAULT_THRESHOLD[metric],
    bandHigh: '',
    unit: DEFAULT_UNIT[info.unitKind],
    window_min: d.window_min !== undefined ? String(d.window_min) : '',
    hold: String(d.hold),
    clear_hold: String(d.clear_hold),
    flap_max: String(d.flap_max),
    flap_window_min: String(d.flap_window_min),
    split: 'none',
    side: 'any',
    direction: 'either',
    kinds: '',
    min_severity: metric === 'netflow_finding' ? 'warning' : '',
    min_confidence: '',
    // Keep only the scope keys the new metric understands.
    scope: Object.fromEntries(Object.entries(scope).filter(([k]) => info.scopeKeys.includes(k as FlowFilterKey))),
  }
}

/** Switch metric, keeping what still applies (scope keys, unit kind). */
export function changeMetric(form: NetflowRuleForm, metric: NetflowMetric): NetflowRuleForm {
  if (metric === form.metric) return form
  return defaultNetflowForm(metric, form.scope)
}

function numText(v: unknown, fallback: string): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : fallback
}

/** Editor state from a stored rule (`metric`, `operator`, `threshold`, `flow_scope`, `flow_params`). */
export function formFromRule(rule: {
  metric: string
  operator?: string | null
  threshold?: number | null
  flow_scope?: Record<string, unknown> | null
  flow_params?: Record<string, unknown> | null
}): NetflowRuleForm {
  const metric = (isNetflowMetric(rule.metric) ? rule.metric : 'netflow_traffic_bps') as NetflowMetric
  const info = NETFLOW_METRIC_BY_KEY[metric]
  const base = defaultNetflowForm(metric)
  const p = (rule.flow_params ?? {}) as Record<string, unknown>
  const op = String(rule.operator ?? '>')
  const mode: ConditionMode = op === '!=' ? 'outside' : op === '<' || op === '<=' ? 'below' : 'above'
  const thr = typeof rule.threshold === 'number' ? rule.threshold : Number(base.threshold)
  const unit =
    info.unitKind === 'percent' || info.unitKind === 'none'
      ? DEFAULT_UNIT[info.unitKind]
      : bestUnit(info.unitKind, mode === 'outside' ? Math.min(thr, Number(p.band_high ?? thr)) : thr)
  const scope: FlowFilterDraft = {}
  for (const [k, v] of Object.entries(rule.flow_scope ?? {})) {
    if (v !== null && v !== undefined && v !== '') scope[k as FlowFilterKey] = String(v)
  }
  return {
    ...base,
    mode,
    inclusive: op === '>=' || op === '<=',
    unit,
    threshold: metric === 'netflow_finding' ? '1' : fromBase(info.unitKind, thr, unit),
    bandHigh: typeof p.band_high === 'number' ? fromBase(info.unitKind, p.band_high, unit) : '',
    window_min: numText(p.window_min, base.window_min),
    hold: numText(p.hold, base.hold),
    clear_hold: numText(p.clear_hold, base.clear_hold),
    flap_max: numText(p.flap_max, base.flap_max),
    flap_window_min: numText(p.flap_window_min, base.flap_window_min),
    split: p.split === 'exporter' ? 'exporter' : 'none',
    side: p.side === 'src' || p.side === 'dst' ? p.side : 'any',
    direction: p.direction === 'in' || p.direction === 'out' ? p.direction : 'either',
    kinds: Array.isArray(p.kinds) ? (p.kinds as string[]).join(', ') : '',
    min_severity: typeof p.min_severity === 'string' ? p.min_severity : metric === 'netflow_finding' ? '' : '',
    min_confidence: typeof p.min_confidence === 'string' ? p.min_confidence : '',
    scope,
    extra: Object.fromEntries(
      PRESERVED_PARAMS.filter((k) => typeof p[k] === 'number' && Number.isFinite(p[k] as number)).map((k) => [k, p[k]]),
    ),
  }
}

export type NetflowFormErrors = Partial<Record<keyof NetflowRuleForm | FlowFilterKey | 'scope', string>>

function wholeInRange(text: string, lo: number, hi: number): boolean {
  if (!/^\d+$/.test(text.trim())) return false
  const n = Number(text)
  return n >= lo && n <= hi
}

/** Field messages; empty when the form can be saved. Mirrors the server's checks. */
export function validateNetflowForm(form: NetflowRuleForm): NetflowFormErrors {
  const e: NetflowFormErrors = {}
  const info = NETFLOW_METRIC_BY_KEY[form.metric]
  if (form.metric !== 'netflow_finding') {
    const thr = toBase(info.unitKind, form.threshold, form.unit)
    if (thr === null) e.threshold = 'Enter a number'
    else if (thr < 0) e.threshold = 'Must not be negative'
    else if (form.metric === 'netflow_exporter_silent' && thr < 60) e.threshold = 'At least 1 minute'
    else if (form.metric === 'netflow_interface_util_pct' && thr > 1000) e.threshold = 'At most 1000%'
    if (form.mode === 'outside') {
      const high = toBase(info.unitKind, form.bandHigh, form.unit)
      if (high === null) e.bandHigh = 'Enter the upper bound'
      else if (thr !== null && high <= thr) e.bandHigh = 'Must be above the lower bound'
    }
    if (!info.modes.includes(form.mode)) e.mode = `${info.label} supports: ${info.modes.join(', ')}`
  }
  if (info.windowRange) {
    const [lo, hi] = info.windowRange
    if (!wholeInRange(form.window_min, lo, hi)) e.window_min = `A whole number of minutes, ${lo}–${hi}`
    else if (info.windowStep > 1 && Number(form.window_min) % info.windowStep) {
      e.window_min = `A multiple of ${info.windowStep} minutes (5-minute interface slots)`
    }
  }
  if (!wholeInRange(form.hold, 1, 60)) e.hold = 'A whole number, 1–60'
  if (!wholeInRange(form.clear_hold, 1, 60)) e.clear_hold = 'A whole number, 1–60'
  if (!wholeInRange(form.flap_max, 0, 50) || form.flap_max.trim() === '1') e.flap_max = '0 (off) or 2–50 state changes'
  if (!wholeInRange(form.flap_window_min, 5, 1440)) e.flap_window_min = 'A whole number of minutes, 5–1440'

  const extra = Object.keys(form.scope).filter(
    (k) => (form.scope[k as FlowFilterKey] ?? '').trim() !== '' && !info.scopeKeys.includes(k as FlowFilterKey),
  )
  if (extra.length) e.scope = `${info.label} rules cannot be scoped by ${extra.map((k) => FLOW_FILTER_LABELS[k as FlowFilterKey] ?? k).join(', ')}`
  const parsed = flowFilterFromDraft(form.scope)
  for (const [k, msg] of Object.entries(parsed.errors)) e[k as FlowFilterKey] = msg
  if (form.metric === 'netflow_interface_util_pct' && parsed.filter.iface) {
    const ref = parseIfaceValue(parsed.filter.iface)
    if (ref && !ref.exporter && !parsed.filter.exporter) e.iface = 'Pick the exporter the interface belongs to'
  }
  if (form.metric === 'netflow_finding' && form.kinds.split(',').some((k) => k.trim().length > 48)) {
    e.kinds = 'Kinds are at most 48 characters each'
  }
  return e
}

export interface NetflowPayload {
  metric: NetflowMetric
  operator: string
  threshold: number
  flow_scope: Record<string, string>
  flow_params: Record<string, unknown>
}

/** The API fields for a valid form (call `validateNetflowForm` first). */
export function netflowPayload(form: NetflowRuleForm): NetflowPayload {
  const info = NETFLOW_METRIC_BY_KEY[form.metric]
  const scope: Record<string, string> = {}
  for (const k of FLOW_FILTER_KEYS) {
    const v = (form.scope[k] ?? '').trim()
    if (v !== '' && info.scopeKeys.includes(k)) scope[k] = v
  }
  const params: Record<string, unknown> = {
    ...(form.extra ?? {}),
    hold: Number(form.hold),
    clear_hold: Number(form.clear_hold),
    flap_max: Number(form.flap_max),
    flap_window_min: Number(form.flap_window_min),
  }
  if (info.windowRange) params.window_min = Number(form.window_min)
  if (form.metric === 'netflow_finding') {
    params.kinds = form.kinds.split(',').map((k) => k.trim()).filter(Boolean)
    params.min_severity = form.min_severity || null
    params.min_confidence = form.min_confidence || null
    return { metric: form.metric, operator: '==', threshold: 1, flow_scope: scope, flow_params: params }
  }
  const threshold = toBase(info.unitKind, form.threshold, form.unit) ?? 0
  let operator = form.mode === 'below' ? (form.inclusive ? '<=' : '<') : form.inclusive ? '>=' : '>'
  if (form.mode === 'outside') {
    operator = '!='
    params.band_high = toBase(info.unitKind, form.bandHigh, form.unit)
  }
  if (form.metric === 'netflow_traffic_bps') params.split = form.split
  if (form.metric === 'netflow_endpoint_bytes') params.side = form.side
  if (form.metric === 'netflow_interface_util_pct') params.direction = form.direction
  return { metric: form.metric, operator, threshold, flow_scope: scope, flow_params: params }
}

/** The applied scope as a typed FlowFilter (for chips and links). */
export function scopeFilter(form: NetflowRuleForm): FlowFilter {
  return flowFilterFromDraft(form.scope).filter
}

/** One line for the review step and the rule list. */
export function summarizeNetflowForm(form: NetflowRuleForm): string {
  const info = NETFLOW_METRIC_BY_KEY[form.metric]
  if (form.metric === 'netflow_finding') {
    const parts = [
      form.kinds.trim() ? `kinds ${form.kinds.trim()}` : 'any kind',
      form.min_severity ? `severity ≥ ${form.min_severity}` : null,
      form.min_confidence ? `confidence ≥ ${form.min_confidence}` : null,
    ].filter(Boolean)
    return `Open detection finding (${parts.join(', ')})`
  }
  const thr = toBase(info.unitKind, form.threshold, form.unit)
  const cond =
    form.mode === 'outside'
      ? `outside ${formatThreshold(info.unitKind, thr)} – ${formatThreshold(info.unitKind, toBase(info.unitKind, form.bandHigh, form.unit))}`
      : `${form.mode === 'below' ? (form.inclusive ? 'at or below' : 'below') : form.inclusive ? 'at or above' : 'above'} ${formatThreshold(info.unitKind, thr)}`
  const win = info.windowRange ? ` over ${form.window_min} min` : ''
  const hold = Number(form.hold) > 1 ? `, held for ${form.hold} evaluations` : ''
  if (form.metric === 'netflow_exporter_silent') return `Exporter silent for more than ${formatThreshold('seconds', thr)}${hold}`
  return `${info.short} ${cond}${win}${hold}`
}

/** Readable scope ("Exporter 10.0.0.1 · Application DNS" / "All visible flow traffic"). */
export function describeScopeDraft(form: NetflowRuleForm): string {
  const parts: string[] = []
  for (const k of FLOW_FILTER_KEYS) {
    const v = (form.scope[k] ?? '').trim()
    if (v) parts.push(`${FLOW_FILTER_LABELS[k]} ${v}`)
  }
  if (parts.length) return parts.join(' · ')
  if (form.metric === 'netflow_exporter_silent') return 'Every flow exporter'
  if (form.metric === 'netflow_finding') return 'All detection findings'
  return 'All flow traffic'
}

/** Reasons a reading is unknown, in words (the dry-run panel). */
export const UNKNOWN_REASON_LABELS: Record<string, string> = {
  no_coverage: 'No flow records from this exporter in the window',
  speed_unknown: 'Interface speed unknown — set a circuit speed or override',
  no_samples: 'No complete 5-minute sample in the window',
  not_ranked: 'Below the ranked list; exact value not computed',
  unknown_clock: 'Receipt time unknown',
}
