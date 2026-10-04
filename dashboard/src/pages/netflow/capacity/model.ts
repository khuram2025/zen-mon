/**
 * Capacity page model (B4): response shapes of `/netflow/capacity*` and the pure display rules
 * the page follows. Kept free of React so `scripts/test-netflow-capacity.mjs` can test it.
 *
 * Rules that come from the review:
 *  - NF-15: headroom is remaining capacity (bps and %), so a fuller bar means MORE room; an
 *    over-capacity value is shown with its real number and an explanation, never clamped; an
 *    unknown speed is "Unknown", never a bar or a percentage.
 *  - NF-16: unknown capacity always comes with the next action (link the exporter, inventory the
 *    device, find the interface, set the circuit speed).
 *  - NF-13: a percentile is labelled with its sample count; when it cannot be stated the peak is
 *    shown and says so.
 */
import { formatBps, formatCount, formatPercent } from '@/lib/netflow/format'
import type { FlowFilter } from '@/lib/netflow/filters'
import { buildNetflowLink } from '@/lib/netflow/links'
import type { NfWindow } from '@/lib/netflow/window'
import type { StatusTone } from '@/components/netflow/StatusTile'

export type CapacityState = 'over' | 'critical' | 'warning' | 'healthy' | 'insufficient' | 'unknown'
export type Resolution = 'resolved' | 'exporter_unmapped' | 'no_interface_inventory' | 'interface_not_found' | 'speed_unknown'
export type SpeedSource = 'configured' | 'override' | 'snmp' | 'sflow' | 'unknown'
export type Direction = 'in' | 'out'
export type InterfaceRole = 'wan' | 'lan' | 'uplink' | 'internal' | 'ignore'

export const STATES: CapacityState[] = ['over', 'critical', 'warning', 'healthy', 'insufficient', 'unknown']
export const RESOLUTIONS: Resolution[] = ['resolved', 'exporter_unmapped', 'no_interface_inventory', 'interface_not_found', 'speed_unknown']
export const ROLES: InterfaceRole[] = ['wan', 'lan', 'uplink', 'internal', 'ignore']

export interface DirectionStats {
  avg_bps: number | null
  p_bps: number | null
  max_bps: number | null
  p_reason: string | null
  speed_bps: number | null
  utilization_avg_pct: number | null
  utilization_p_pct: number | null
  utilization_max_pct: number | null
  headroom_bps: number | null
  headroom_pct: number | null
  bytes: number | null
  packets: number | null
}

export interface Headline {
  direction: Direction
  /** 'p95' (the configured percentile) or 'max' when the percentile cannot be stated. */
  statistic: string
  value_bps: number | null
  utilization_pct: number | null
  headroom_bps: number | null
  headroom_pct: number | null
  state: CapacityState
  reason: string | null
  note: string | null
  /** Exactly one direction has a known speed (NF-16): the interface is never called healthy then. */
  partial_speed?: boolean
  /** Directions without a known speed. */
  unknown_directions?: Direction[]
}

export interface InterfaceOverride {
  exporter: string
  ifindex: number
  display_name: string | null
  speed_in_bps: number | null
  speed_out_bps: number | null
  role: InterfaceRole | null
  note: string | null
  updated_at: string | null
  updated_by: string | null
}

export interface CrossCheck {
  status: 'ok' | 'no_data' | 'no_device' | 'not_in_inventory' | 'unavailable'
  statistic?: string
  samples?: number
  in_p_bps?: number | null
  out_p_bps?: number | null
  in_max_bps?: number | null
  out_max_bps?: number | null
  in_delta_pct?: number | null
  out_delta_pct?: number | null
  in_utilization_pct?: number | null
  out_utilization_pct?: number | null
  if_speed?: number | null
}

export interface CapacityRow {
  key: string
  exporter: string
  ifindex: number
  display_name: string
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  device_id: string | null
  device_hostname: string | null
  owner_source: 'mapped' | 'ip-match' | null
  interface_in_inventory: boolean
  device_interface_count: number
  role: InterfaceRole | null
  override: InterfaceOverride | null
  resolution: Resolution
  speed: {
    in_bps: number | null
    out_bps: number | null
    source_in: SpeedSource
    source_out: SpeedSource
    snmp_bps: number | null
    configured_bps: number | null
    override_in_bps: number | null
    override_out_bps: number | null
    sflow_bps: number | null
    /** Directions whose stored NetFlow override speed is not in use: the device circuit speed takes precedence. */
    override_shadowed?: Direction[]
  }
  samples: number
  expected_slots: number
  gap_slots: number
  coverage_pct: number | null
  in: DirectionStats
  out: DirectionStats
  combined: { avg_bps: number | null; p_bps: number | null; max_bps: number | null; bytes: number | null }
  billable: { bps: number | null; direction: Direction | 'merged' | null; mode: string }
  headline: Headline
  last_slot: string | null
  previous: {
    from: string
    to: string
    value_bps: number | null
    utilization_pct: number | null
    change_pct: number | null
    samples: number
    reason: string | null
  } | null
  snmp?: CrossCheck
  counters?: CrossCheck
  /** The exporter's newest slots were still settling: samples end here (ISO), not at the window end. */
  settled_until?: string | null
}

export interface CapacitySummary {
  interfaces_total: number
  speed_known: number
  speed_unknown: number
  by_resolution: Record<Resolution, number>
  by_state: Record<CapacityState, number>
  exporters_unmapped: string[]
}

export interface CapacityMethod {
  statistic: string
  percentile: number
  percentile_definition: string
  min_samples: number
  min_coverage_pct: number
  direction_mode: 'separate' | 'merged'
  direction_mode_definition: string
  speed_rule: string
  thresholds: { warn_pct: number; crit_pct: number }
  business_hours: BusinessHours | null
  idle: string
  gaps: string
  flow_attribution: string
  dedup: string
  [k: string]: unknown
}

export interface BusinessHours {
  enabled: boolean
  start_hour: number
  end_hour: number
  days: number[]
  tz: string
}

export interface CapacitySettings {
  percentile: number
  direction_mode: 'separate' | 'merged'
  business_hours: BusinessHours
  warn_pct: number
  crit_pct: number
  min_coverage_pct: number
  billing_min_coverage_pct: number
  stored?: boolean
  updated_at?: string | null
  updated_by?: string | null
}

export interface CapacityPermissions {
  manage: boolean
  device_speed: boolean
}

export interface CapacityData {
  window: {
    from: string
    to: string
    tz: string
    first_slot: string
    end_exclusive: string
    slot_seconds: number
    complete_slots: number
    expected_slots: number
    head_excluded_s: number
    tail_excluded_s: number
    retention_cut: boolean
    business_hours: boolean
    raw_from: string | null
    previous: { from: string; to: string } | null
    /** Exporters whose newest slots are still settling and were left out. */
    provisional?: { exporter: string; settled_until: string; provisional_slots: number; active_timeout_s: number; basis: 'observed' | 'default' | 'sflow' }[]
  }
  method: CapacityMethod
  settings: CapacitySettings
  permissions: CapacityPermissions
  summary: CapacitySummary
  matched: number
  offset: number
  limit: number
  sort: string
  order: 'asc' | 'desc'
  items: CapacityRow[]
  exporters: { exporter: string; count_direction: string; directions_seen: number[]; accounting_basis: string }[]
}

export interface ForecastResult {
  status: 'ok' | 'insufficient_history' | 'not_growing' | 'speed_unknown' | 'beyond_horizon'
  reason: string | null
  days_used: number
  min_days: number
  slope_bps_per_day: number | null
  r2: number | null
  confidence: 'high' | 'medium' | 'low' | null
  fitted_now_bps: number | null
  days_to_warn: number | null
  days_to_crit: number | null
  days_to_full: number | null
}

export interface TrendDayDirection {
  p_bps: number | null
  max_bps: number | null
  avg_bps: number | null
  utilization_p_pct: number | null
  utilization_max_pct?: number | null
}

/** The speeds in effect on a day (FE-05): reconstructed from the audited circuit-speed and override changes. */
export interface TrendDaySpeed {
  in_bps: number | null
  out_bps: number | null
  source_in: SpeedSource
  source_out: SpeedSource
  /** A speed change was audited during this day. */
  changed?: boolean
}

export interface TrendDay {
  day: string
  complete: boolean
  samples: number
  expected_slots: number
  coverage_pct: number | null
  p_reason: string | null
  speed?: TrendDaySpeed
  in: TrendDayDirection
  out: TrendDayDirection
}

/** One audited speed change of the interface, per direction (null side: that direction did not change). */
export interface SpeedChange {
  at: string | null
  action: string
  source: 'configured' | 'override' | string
  in?: { old: number | null; new: number | null } | null
  out?: { old: number | null; new: number | null } | null
  /** Both directions' old/new when they are the same (always for the circuit speed). */
  old: number | null
  new: number | null
  actor: string | null
  changes: Record<string, { old: unknown; new: unknown }> | null
}

export interface TrendData {
  exporter: string
  ifindex: number
  display_name: string
  device_id: string | null
  device_hostname: string | null
  resolution: Resolution
  speed: CapacityRow['speed']
  window: { first_slot: string; end_exclusive: string; tz: string; days: number }
  method: CapacityMethod & { forecast: string }
  days: TrendDay[]
  forecast: { in: ForecastResult; out: ForecastResult; headline_direction: Direction }
  speed_history: SpeedChange[]
}

export interface BillingItem {
  exporter: string
  ifindex: number
  display_name: string
  if_alias: string | null
  device_hostname: string | null
  role: InterfaceRole | null
  samples: number
  expected_slots: number
  coverage_pct: number | null
  in_p_bps: number | null
  out_p_bps: number | null
  combined_p_bps: number | null
  in_max_bps: number | null
  out_max_bps: number | null
  billable_bps: number | null
  billable_direction: Direction | 'merged' | null
  speed_in_bps: number | null
  speed_out_bps: number | null
  billable_util_pct: number | null
  billing_grade: boolean
  notes: string[]
  p_reason: string | null
}

export interface BillingData {
  period: { from: string; to: string; tz: string; month: string | null; complete: boolean; expected_slots: number; retention_cut: boolean }
  method: CapacityMethod & { billing_grade_rule: string; billing_min_coverage_pct: number; overridden: string[] }
  settings: CapacitySettings
  items: BillingItem[]
  totals: { interfaces: number; billing_grade: number; indicative: number }
}

/* ─── Labels and tones ─────────────────────────────────────────────── */

export const STATE_LABELS: Record<CapacityState, string> = {
  over: 'Over capacity',
  critical: 'Critical',
  warning: 'Warning',
  healthy: 'Healthy',
  insufficient: 'Insufficient data',
  unknown: 'Speed unknown',
}

export function stateTone(state: CapacityState): StatusTone {
  if (state === 'over' || state === 'critical') return 'danger'
  if (state === 'warning') return 'warning'
  if (state === 'healthy') return 'success'
  return 'unknown'
}

/** Text colour class for a utilization value in a given state (the `*-fg` tokens, NF-29). */
export function stateTextClass(state: CapacityState): string {
  if (state === 'over' || state === 'critical') return 'text-danger-fg'
  if (state === 'warning') return 'text-warning-fg'
  if (state === 'healthy') return 'text-success-fg'
  return 'text-text2'
}

export const RESOLUTION_LABELS: Record<Resolution, string> = {
  resolved: 'Capacity known',
  exporter_unmapped: 'Exporter not linked to a device',
  no_interface_inventory: 'Device has no interface inventory',
  interface_not_found: 'Interface not in the device inventory',
  speed_unknown: 'Interface speed unknown',
}

export const RESOLUTION_SHORT: Record<Resolution, string> = {
  resolved: 'Known',
  exporter_unmapped: 'Exporter not linked',
  no_interface_inventory: 'No inventory',
  interface_not_found: 'Interface not found',
  speed_unknown: 'Speed unknown',
}

export const SOURCE_LABELS: Record<SpeedSource, string> = {
  configured: 'configured circuit',
  override: 'NetFlow override',
  snmp: 'SNMP ifSpeed',
  sflow: 'sFlow ifSpeed',
  unknown: 'unknown',
}

/** Compact speed-source words for table cells. */
export const SOURCE_SHORT: Record<SpeedSource, string> = {
  configured: 'circuit',
  override: 'override',
  snmp: 'SNMP',
  sflow: 'sFlow',
  unknown: 'unknown',
}

export const ROLE_LABELS: Record<InterfaceRole, string> = {
  wan: 'WAN',
  lan: 'LAN',
  uplink: 'Uplink',
  internal: 'Internal',
  ignore: 'Ignored',
}

export const DIRECTION_LABELS: Record<Direction, string> = { in: 'In (received)', out: 'Out (transmitted)' }

/** "p95" or "Peak": what the headline number is. */
export function statisticLabel(statistic: string): string {
  return statistic === 'max' ? 'Peak' : statistic
}

const P_REASONS: Record<string, string> = {
  too_few_samples: 'too few samples for a percentile',
  low_coverage: 'collection coverage too low for a percentile',
  no_samples: 'no samples',
}

export function pReasonText(reason: string | null | undefined): string | null {
  return reason ? P_REASONS[reason] ?? reason.replace(/_/g, ' ') : null
}

/** "p95 of 2,015 samples · 100% coverage" / "Peak of 12 samples (too few samples for a percentile)". */
export function samplesText(row: Pick<CapacityRow, 'samples' | 'coverage_pct' | 'headline'>): string {
  const cov = row.coverage_pct != null ? ` · ${formatPercent(row.coverage_pct)} coverage` : ''
  const stat = statisticLabel(row.headline.statistic)
  const why = row.headline.statistic === 'max' ? pReasonText(row.headline.reason) : null
  return `${stat} of ${formatCount(row.samples)} sample${row.samples === 1 ? '' : 's'}${cov}${why ? ` (${why})` : ''}`
}

/** Compact sample figures for a table cell: "2,015 of 2,016" and "100% coverage". */
export function samplesShort(row: Pick<CapacityRow, 'samples' | 'expected_slots' | 'coverage_pct'>): { count: string; coverage: string } {
  return {
    count: `${formatCount(row.samples)} of ${formatCount(row.expected_slots)}`,
    coverage: row.coverage_pct != null ? `${formatPercent(row.coverage_pct)} coverage` : 'coverage unknown',
  }
}

/** Why the headline is a peak rather than the percentile, or null when it is the percentile. */
export function peakNote(h: Pick<Headline, 'statistic' | 'reason'>): string | null {
  if (h.statistic !== 'max') return null
  return h.reason === 'low_coverage' ? 'Peak: coverage too low for a percentile' : h.reason === 'too_few_samples' ? 'Peak: too few samples for a percentile' : 'Peak'
}

/* ─── Headroom (NF-15) ─────────────────────────────────────────────── */

export interface HeadroomView {
  kind: 'ok' | 'over' | 'unknown' | 'insufficient'
  /** Remaining capacity, % of the speed (negative when over). */
  headroomPct: number | null
  headroomBps: number | null
  util: number | null
  /** Bar width 0..100: the REMAINING share, so a fuller bar means more room. Null: draw no bar. */
  barPct: number | null
  text: string
  /** Second line (over capacity: the utilization, never clamped). */
  sub: string | null
  title: string
}

export function headroomView(
  h: Pick<Headline, 'utilization_pct' | 'headroom_bps' | 'headroom_pct' | 'value_bps' | 'state'> &
    Partial<Pick<Headline, 'direction' | 'partial_speed' | 'unknown_directions'>>,
): HeadroomView {
  if (h.value_bps == null) {
    return { kind: 'insufficient', headroomPct: null, headroomBps: null, util: null, barPct: null, text: 'No samples', sub: null, title: 'No samples in this window' }
  }
  if (h.state === 'insufficient') {
    return {
      kind: 'insufficient', headroomPct: null, headroomBps: null, util: null, barPct: null, text: 'Too little data', sub: null,
      title: 'Collection coverage is too low to state the remaining capacity: the gaps may hold higher traffic',
    }
  }
  if (h.utilization_pct == null || h.headroom_pct == null || h.headroom_bps == null) {
    return {
      kind: 'unknown', headroomPct: null, headroomBps: null, util: null, barPct: null, text: 'Unknown', sub: null,
      title: 'The interface speed is unknown, so remaining capacity cannot be calculated',
    }
  }
  if (h.partial_speed && h.state === 'unknown') {
    // One direction has no known speed (NF-16): its share of the link is unknown, so the interface's
    // remaining capacity is too. The known direction's room is a detail, never "healthy".
    const missing = unknownDirectionsText(h.unknown_directions)
    const known = h.direction ? `${h.direction === 'in' ? 'In' : 'Out'}: ` : ''
    return {
      kind: 'unknown',
      headroomPct: null,
      headroomBps: null,
      util: null,
      barPct: null,
      text: `${missing} speed unknown`,
      sub: `${known}${formatBps(h.headroom_bps)} free (${formatPercent(h.headroom_pct)})`,
      title: `The ${missing.toLowerCase()} speed is unknown, so that direction's use of the link and the interface's remaining capacity cannot be stated. Set the speed for both directions.`,
    }
  }
  if (h.utilization_pct > 100) {
    return {
      kind: 'over',
      headroomPct: h.headroom_pct,
      headroomBps: h.headroom_bps,
      util: h.utilization_pct,
      barPct: null,
      text: `Over by ${formatBps(-h.headroom_bps)}`,
      sub: `${formatPercent(h.utilization_pct)} of the speed`,
      title:
        'The measured value exceeds the interface speed. Check the circuit speed or override, duplicate ingress and egress export, and the sampling rate.',
    }
  }
  const pct = Math.max(0, Math.min(100, h.headroom_pct))
  return {
    kind: 'ok',
    headroomPct: h.headroom_pct,
    headroomBps: h.headroom_bps,
    util: h.utilization_pct,
    barPct: pct,
    text: `${formatBps(h.headroom_bps)} free (${formatPercent(h.headroom_pct)})`,
    sub: null,
    title: `${formatPercent(h.utilization_pct)} used, ${formatPercent(h.headroom_pct)} of the capacity remains`,
  }
}

/** "Out" / "In" / "In and out" for the directions without a known speed. */
export function unknownDirectionsText(dirs: Direction[] | null | undefined): string {
  const d = dirs ?? []
  if (d.length === 1) return d[0] === 'in' ? 'In' : 'Out'
  return 'In and out'
}

/* ─── Speed sources (NF-16) ────────────────────────────────────────── */

/**
 * The stored NetFlow override speeds that are not in use because the device circuit speed takes
 * precedence (an override that silently did nothing was a trap), or null when nothing is shadowed.
 */
export function overrideShadowNote(speed: Pick<CapacityRow['speed'], 'configured_bps' | 'override_in_bps' | 'override_out_bps' | 'override_shadowed'>): string | null {
  const dirs = speed.override_shadowed ?? []
  if (!dirs.length || !speed.configured_bps) return null
  const parts = dirs.map((d) => `${d} ${formatBps(d === 'in' ? speed.override_in_bps : speed.override_out_bps)}`)
  return `NetFlow override (${parts.join(', ')}) not in use: the device circuit speed ${formatBps(speed.configured_bps)} takes precedence`
}

/** The override form's warning while a device circuit speed is set: the override would not apply. */
export function overrideFormNote(configuredBps: number | null | undefined): string | null {
  if (!configuredBps) return null
  return `Not in use while the device circuit speed (${formatBps(configuredBps)}) is set: it takes precedence in both directions. Clear the circuit speed to use per-direction override speeds; the name, role and note apply either way.`
}

/* ─── Cross-checks ─────────────────────────────────────────────────── */

/**
 * The SNMP/sFlow cross-check for a table cell. The API's `*_delta_pct` is (flow − SNMP) / SNMP, so
 * it reads "flow is X% vs SNMP" — never "SNMP is X% vs flow", which inverted its sign.
 */
export function crossCheckText(snmp?: CrossCheck, counters?: CrossCheck): { text: string; title: string } | null {
  if (snmp?.status === 'ok') {
    const worst = [snmp.in_delta_pct, snmp.out_delta_pct].filter((x): x is number => x != null)
    const d = worst.length ? worst.reduce((a, b) => (Math.abs(b) > Math.abs(a) ? b : a)) : null
    return {
      text: d == null ? 'SNMP ok' : `Flow ${changeText(d)} vs SNMP`,
      title: `Flow ${snmp.statistic} against SNMP counters over the same slots: flow is ${changeText(snmp.in_delta_pct)} (in) and ${changeText(snmp.out_delta_pct)} (out) vs SNMP (${formatCount(snmp.samples ?? 0)} SNMP samples)`,
    }
  }
  if (counters?.status === 'ok') {
    const u = [counters.in_utilization_pct, counters.out_utilization_pct].filter((x): x is number => x != null)
    return {
      text: u.length ? `sFlow ${formatPercent(Math.max(...u))}` : `sFlow ${formatBps(Math.max(counters.in_p_bps ?? counters.in_max_bps ?? 0, counters.out_p_bps ?? counters.out_max_bps ?? 0))}`,
      title: `Measured by sFlow interface counters (${counters.statistic}): in ${formatBps(counters.in_p_bps ?? counters.in_max_bps)}, out ${formatBps(counters.out_p_bps ?? counters.out_max_bps)}`,
    }
  }
  return null
}

/** The dialog sentence for the SNMP cross-check: SNMP's own figures, then how far flow is from them. */
export function snmpSentence(snmp?: CrossCheck): string | null {
  if (snmp?.status !== 'ok') return null
  const inV = formatBps(snmp.in_p_bps ?? snmp.in_max_bps)
  const outV = formatBps(snmp.out_p_bps ?? snmp.out_max_bps)
  return `SNMP counters (${snmp.statistic}): in ${inV}, out ${outV}; flow is ${changeText(snmp.in_delta_pct)} (in) and ${changeText(snmp.out_delta_pct)} (out) vs SNMP.`
}

/* ─── Speed history and per-day capacity (FE-05) ───────────────────── */

function pairText(p: { old: number | null; new: number | null }): string {
  return `${p.old ? formatBps(p.old) : 'none'} → ${p.new ? formatBps(p.new) : 'none'}`
}

/** "circuit speed: none → 20 Mbps (bulk change)" / "NetFlow override: in none → 50 Mbps, out none → 20 Mbps". */
export function speedChangeText(e: SpeedChange): string {
  const bulk = /bulk/.test(e.action) ? ' (bulk change)' : ''
  if (e.source === 'configured') {
    const p = e.in ?? { old: e.old, new: e.new }
    return `circuit speed: ${pairText(p)}${bulk}`
  }
  const i = e.in ?? null
  const o = e.out ?? null
  if (i && o && i.old === o.old && i.new === o.new) return `NetFlow override: ${pairText(i)} (both directions)${bulk}`
  const parts = [i ? `in ${pairText(i)}` : null, o ? `out ${pairText(o)}` : null].filter(Boolean)
  if (parts.length) return `NetFlow override: ${parts.join(', ')}${bulk}`
  if (e.old != null || e.new != null) return `NetFlow override: ${pairText({ old: e.old, new: e.new })}${bulk}`
  return `NetFlow override changed${bulk}`
}

export type TrendMode = 'bps' | 'pct'

export interface TrendPoint {
  day: string
  in: number | null
  out: number | null
  speedIn: number | null
  speedOut: number | null
  /** Symmetric circuits only: one speed and its warning/critical levels, per day (step lines). */
  speed: number | null
  warn: number | null
  crit: number | null
  changed: boolean
}

export interface TrendModel {
  mode: TrendMode
  points: TrendPoint[]
  /** Every day's in and out speeds are equal: one speed line with levels; else a line per direction. */
  symmetric: boolean
  anySpeed: boolean
  speedChanged: boolean
  yMax: number
  /** Legend/label text per speed line. */
  speedLabels: { speed: string; speedIn: string; speedOut: string }
}

function speedRangeText(values: (number | null)[]): string {
  const known = values.filter((v): v is number => v != null && v > 0)
  if (!known.length) return 'unknown'
  const lo = Math.min(...known)
  const hi = Math.max(...known)
  return lo === hi ? formatBps(lo) : `${formatBps(lo)}–${formatBps(hi)}`
}

/**
 * The daily trend chart (FE-05): each direction against its own capacity, day by day. In bit/s,
 * an asymmetric circuit (50/20 Mbps) gets a speed line per direction in the direction's colour,
 * never one line at the larger speed; a symmetric one keeps a single speed line with the warning
 * and critical levels. In percent, each day's value is divided by that day's speed, so a speed
 * upgrade does not rewrite the utilization of the days before it.
 */
export function trendModel(data: Pick<TrendData, 'days' | 'speed'>, levels: { warn: number; crit: number }, mode: TrendMode): TrendModel {
  const points: TrendPoint[] = data.days.map((d) => {
    const sIn = d.speed ? d.speed.in_bps : data.speed.in_bps
    const sOut = d.speed ? d.speed.out_bps : data.speed.out_bps
    const v = (x: TrendDayDirection) => (d.complete ? (mode === 'pct' ? x.utilization_p_pct : x.p_bps) : null)
    return {
      day: d.day, in: v(d.in), out: v(d.out),
      speedIn: sIn || null, speedOut: sOut || null,
      speed: null, warn: null, crit: null,
      changed: !!d.speed?.changed,
    }
  })
  const anySpeed = points.some((p) => p.speedIn || p.speedOut)
  const symmetric = points.every((p) => p.speedIn === p.speedOut)
  if (mode === 'bps' && symmetric) {
    for (const p of points) {
      p.speed = p.speedIn
      p.warn = p.speedIn ? (p.speedIn * levels.warn) / 100 : null
      p.crit = p.speedIn ? (p.speedIn * levels.crit) / 100 : null
    }
  }
  const values = points.flatMap((p) => [p.in ?? 0, p.out ?? 0])
  const caps = mode === 'pct' ? [100] : points.flatMap((p) => [p.speedIn ?? 0, p.speedOut ?? 0])
  return {
    mode,
    points,
    symmetric,
    anySpeed,
    speedChanged: points.some((p) => p.changed) || new Set(points.map((p) => `${p.speedIn}/${p.speedOut}`)).size > 1,
    yMax: Math.max(0, ...values, ...caps),
    speedLabels: {
      speed: `Speed ${speedRangeText(points.map((p) => p.speedIn))}`,
      speedIn: `In speed ${speedRangeText(points.map((p) => p.speedIn))}`,
      speedOut: `Out speed ${speedRangeText(points.map((p) => p.speedOut))}`,
    },
  }
}

/** "50.0 Mbps / 20.0 Mbps" (in / out) or one value when equal. */
export function daySpeedText(s: TrendDaySpeed | undefined): string {
  if (!s) return '—'
  if (s.in_bps === s.out_bps) return s.in_bps ? formatBps(s.in_bps) : 'Unknown'
  return `${s.in_bps ? formatBps(s.in_bps) : '?'} / ${s.out_bps ? formatBps(s.out_bps) : '?'}`
}

/**
 * The settling disclosure: an exporter reports a long-lived flow only when it ends or at its active
 * timeout, so its newest slots are left out until then, never counted as idle. Null when none.
 */
export function provisionalText(provisional: CapacityData['window']['provisional']): string | null {
  const p = provisional ?? []
  if (!p.length) return null
  const minutes = Math.max(...p.map((x) => x.provisional_slots)) * 5
  const assumed = p.some((x) => x.basis === 'default')
  return `The newest ${minutes} minutes of ${p.length === 1 ? 'one exporter' : `${p.length} exporters`} are still settling and are left out: long-lived flows are reported only when they end or at the exporter's active timeout (up to ${Math.round(Math.max(...p.map((x) => x.active_timeout_s)) / 60)} min${assumed ? ', assumed where no flow durations were seen' : ''}), so those slots are not known to be idle.`
}

/* ─── Coverage and next actions (NF-16) ────────────────────────────── */

const UNRESOLVED: Exclude<Resolution, 'resolved'>[] = ['exporter_unmapped', 'no_interface_inventory', 'interface_not_found', 'speed_unknown']

const REASON_PHRASE: Record<Exclude<Resolution, 'resolved'>, [string, string]> = {
  exporter_unmapped: ['exporter not linked', 'exporter not linked'],
  no_interface_inventory: ['device without interface inventory', 'devices without interface inventory'],
  interface_not_found: ['interface not in inventory', 'interfaces not in inventory'],
  speed_unknown: ['speed unknown', 'speeds unknown'],
}

/** "4 of 16 interfaces lack a known capacity: 3 exporter not linked, 1 speed unknown". */
export function coverageSentence(s: Pick<CapacitySummary, 'interfaces_total' | 'speed_known' | 'by_resolution'>): string {
  const total = s.interfaces_total
  const unknown = total - s.speed_known
  if (total === 0) return 'No interfaces carried flow traffic in this period'
  if (unknown === 0) return `All ${formatCount(total)} interface${total === 1 ? ' has' : 's have'} a known capacity`
  const parts = UNRESOLVED.filter((r) => (s.by_resolution[r] ?? 0) > 0).map((r) => {
    const n = s.by_resolution[r]
    return `${formatCount(n)} ${REASON_PHRASE[r][n === 1 ? 0 : 1]}`
  })
  return `${formatCount(unknown)} of ${formatCount(total)} interface${total === 1 ? '' : 's'} lack${unknown === 1 ? 's' : ''} a known capacity${parts.length ? `: ${parts.join(', ')}` : ''}`
}

export type ActionKind = 'link_exporter' | 'open_device' | 'open_interfaces' | 'set_speed' | 'none'

export interface ResolutionAction {
  kind: ActionKind
  label: string
  hint: string
  /** The user lacks the permission for the action: show the hint only. */
  disabled: boolean
}

export function resolutionAction(row: Pick<CapacityRow, 'resolution' | 'device_id' | 'interface_in_inventory'>, perms: CapacityPermissions): ResolutionAction {
  switch (row.resolution) {
    case 'exporter_unmapped':
      return {
        kind: 'link_exporter',
        label: 'Link exporter',
        hint: 'Link this exporter to its device so interface names and SNMP speeds resolve, or set the speed as a NetFlow override.',
        disabled: !perms.manage,
      }
    case 'no_interface_inventory':
      return {
        kind: 'open_device',
        label: 'Open device',
        hint: 'The linked device has no interface inventory: enable SNMP polling on it, or set the speed as a NetFlow override.',
        disabled: false,
      }
    case 'interface_not_found':
      return {
        kind: 'open_interfaces',
        label: 'Check interfaces',
        hint: 'This ifIndex is not in the device inventory (re-indexed after a reboot, or a logical interface). Rediscover interfaces, or set the speed as a NetFlow override.',
        disabled: false,
      }
    case 'speed_unknown':
      return {
        kind: 'set_speed',
        label: 'Set speed',
        hint: 'SNMP reports no speed for this interface. Set the circuit speed.',
        disabled: !perms.device_speed && !perms.manage,
      }
    default:
      return { kind: 'none', label: '', hint: '', disabled: true }
  }
}

/** Where a speed change is written: the device interface (shared with link utilization and alerts) or a NetFlow override. */
export function speedTargets(row: Pick<CapacityRow, 'device_id' | 'interface_in_inventory'>, perms: CapacityPermissions): ('device' | 'netflow')[] {
  const out: ('device' | 'netflow')[] = []
  if (row.device_id && row.interface_in_inventory && perms.device_speed) out.push('device')
  if (perms.manage) out.push('netflow')
  return out
}

export const SPEED_UNITS = { kbps: 1e3, Mbps: 1e6, Gbps: 1e9 } as const
export type SpeedUnit = keyof typeof SPEED_UNITS

/** A speed field in bit/s: null for empty (clears), an error string for anything invalid. */
export function parseSpeed(value: string, unit: SpeedUnit): number | null | string {
  const v = value.trim()
  if (v === '') return null
  if (!/^\d+(\.\d+)?$/.test(v)) return 'Enter a number, for example 100'
  const bps = Math.round(Number(v) * SPEED_UNITS[unit])
  if (!(bps > 0)) return 'The speed must be above zero'
  if (bps > 1e15) return 'The speed is too large'
  return bps
}

/** The input text and unit for an existing speed, picking the largest unit that keeps it whole-ish. */
export function speedInput(bps: number | null | undefined): { value: string; unit: SpeedUnit } {
  if (!bps || bps <= 0) return { value: '', unit: 'Mbps' }
  if (bps >= 1e9 && bps % 1e8 === 0) return { value: String(bps / 1e9), unit: 'Gbps' }
  if (bps >= 1e6) return { value: String(Math.round((bps / 1e6) * 1000) / 1000), unit: 'Mbps' }
  return { value: String(Math.round((bps / 1e3) * 1000) / 1000), unit: 'kbps' }
}

/* ─── Change and forecast ──────────────────────────────────────────── */

export function changeText(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  if (Math.abs(pct) < 0.05) return '±0%'
  return `${pct > 0 ? '+' : '−'}${formatPercent(Math.abs(pct))}`
}

export function daysText(days: number | null | undefined): string {
  if (days == null) return '—'
  if (days <= 0) return 'Now'
  if (days < 1) return '< 1 day'
  if (days < 60) return `${Math.round(days)} days`
  return `~${Math.round(days / 30.4)} months`
}

const FORECAST_STATUS: Record<ForecastResult['status'], string> = {
  ok: 'Forecast',
  insufficient_history: 'Insufficient history',
  not_growing: 'Not growing',
  speed_unknown: 'Speed unknown',
  beyond_horizon: 'Beyond two years',
}

export function forecastHeadline(fc: ForecastResult): string {
  if (fc.status === 'ok') {
    if (fc.days_to_full === 0) return 'Already at capacity'
    if (fc.days_to_crit === 0) return 'Already above critical'
    if (fc.days_to_warn === 0) return 'Already above warning'
    const next = [fc.days_to_warn, fc.days_to_crit, fc.days_to_full].find((d) => d != null)
    return next != null ? `Warning in ${daysText(fc.days_to_warn ?? next)}` : 'No level reached'
  }
  return FORECAST_STATUS[fc.status]
}

export function growthText(fc: Pick<ForecastResult, 'slope_bps_per_day'>): string {
  const s = fc.slope_bps_per_day
  if (s == null) return '—'
  const perMonth = s * 30
  return `${perMonth >= 0 ? '+' : '−'}${formatBps(Math.abs(perMonth))} per month`
}

/* ─── Links (FE-02) ────────────────────────────────────────────────── */

/**
 * Drill-downs from one interface: the whole page filter and window, narrowed to this interface
 * on this exporter (the qualified `iface` form). A page `exporter` filter is kept only when it is
 * this interface's exporter (otherwise the link would select nothing).
 */
export function interfaceLinks(
  row: Pick<CapacityRow, 'exporter' | 'ifindex'>,
  filter: FlowFilter,
  w: NfWindow,
): { overview: string; forensics: string } {
  const f: FlowFilter = { ...filter, iface: `${row.ifindex}@${row.exporter}` }
  if (f.exporter && f.exporter !== row.exporter) delete f.exporter
  return {
    overview: buildNetflowLink('/netflow', { filter: f, window: w }),
    forensics: buildNetflowLink('/netflow/forensics', { filter: f, window: w, extra: { run: true } }),
  }
}

/* ─── Sorting options ──────────────────────────────────────────────── */

export const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: 'risk', label: 'Utilization risk' },
  { value: 'util', label: 'Utilization' },
  { value: 'headroom', label: 'Headroom' },
  { value: 'in', label: 'In rate' },
  { value: 'out', label: 'Out rate' },
  { value: 'combined', label: 'Combined throughput' },
  { value: 'change', label: 'Change vs previous period' },
  { value: 'coverage', label: 'Coverage' },
  { value: 'name', label: 'Name' },
]

/** Previous calendar month as YYYY-MM in a zone (billing default). */
export function lastMonth(nowMs: number, tz: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit' }).formatToParts(new Date(nowMs))
  const y = Number(parts.find((p) => p.type === 'year')?.value)
  const m = Number(parts.find((p) => p.type === 'month')?.value)
  const py = m === 1 ? y - 1 : y
  const pm = m === 1 ? 12 : m - 1
  return `${py}-${String(pm).padStart(2, '0')}`
}

export const WEEKDAYS: { iso: number; short: string }[] = [
  { iso: 1, short: 'Mon' }, { iso: 2, short: 'Tue' }, { iso: 3, short: 'Wed' }, { iso: 4, short: 'Thu' },
  { iso: 5, short: 'Fri' }, { iso: 6, short: 'Sat' }, { iso: 7, short: 'Sun' },
]

export function businessHoursText(bh: BusinessHours): string {
  const days = WEEKDAYS.filter((d) => bh.days.includes(d.iso)).map((d) => d.short)
  const pad = (h: number) => `${String(h % 24).padStart(2, '0')}:00`
  return `${days.join(', ')} ${pad(bh.start_hour)}–${pad(bh.end_hour)} (${bh.tz})`
}
