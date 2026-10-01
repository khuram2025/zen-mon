/**
 * Collector & exporter health (B6): response shapes and the pure presentation rules the Health,
 * Setup and mapping views share. No React here, so `scripts/test-netflow-health.mjs` can test it.
 *
 * Everything is judged by the appliance (the API computes states on its own clock); the UI only
 * formats, sorts, summarises and ages values against `serverNow()`.
 */
import type { NfExporterState, NfExporterStatus, NfExporterStatusRow } from '@/lib/netflow/types'
import type { StatusTone } from '@/components/netflow/StatusTile'
import { isIpAddress } from '@/lib/netflow/ip'

/* ─── Shapes (server/app/api/v1/netflow/health.py, exporters.py) ──────────── */

export interface NfIssue {
  code: string
  severity: 'danger' | 'warning' | 'info'
  message: string
}

/** `/exporters/status` row with the B6 extras (the §11 fields are in `NfExporterStatusRow`). */
export interface HealthExporterRow extends NfExporterStatusRow {
  mapping_source?: 'mapped' | 'ip-match' | null
  last_record_at?: string | null
  last_datagram_at?: string | null
  state_since?: string | null
  records_1h?: number | null
  datagrams_15m?: number | null
  interval_basis?: 'configured' | 'learned' | 'default'
  learned_interval_s?: number | null
  late_after_s?: number
  silent_after_s?: number
  clock_direction?: 'ahead' | 'behind' | null
  observation_domains?: number[]
  flag_coverage_pct?: number | null
  sampling_interval?: number | null
  role?: ExporterRole
  count_direction?: CountDirection
  has_settings?: boolean
  issues?: NfIssue[]
}

export interface HealthStatus extends Omit<NfExporterStatus, 'exporters'> {
  exporters: HealthExporterRow[]
  collector?: { reachable: boolean; status: string; error: string | null }
  warnings?: { code: string; message: string }[]
}

export type CollectorStatus = 'ok' | 'degraded' | 'down' | 'unknown'

export interface StatsCounters {
  datagrams: number
  datagram_bytes: number
  records_decoded: number
  records_written: number
  records_dropped_implausible: number
  records_dropped_queue: number
  records_dropped_unknown_exporter: number
  records_unsupported: number
  parse_errors: number
  data_sets_waiting_template: number
  options_records: number
  sequence_gaps: number
  clock_clamped_records: number
  insert_failures: number
  insert_retries: number
  kernel_rcvbuf_errors: number
  /** Records stored unscaled: their sampler rate was ambiguous (X-08). Absent before the column existed. */
  sampling_ambiguous_records?: number
}

export interface SeriesPoint {
  ts: string
  datagrams: number
  records_decoded: number
  records_written: number
  dropped: number
  waiting: number
  sequence_gaps: number
  parse_errors: number
  insert_failures: number
  queue_len: number
}

export interface CollectorHealth {
  server_time: string
  window: { from: string; to: string; granularity_s: number; tz?: string; mode?: string }
  live: {
    reachable: boolean
    error: string | null
    fetched_at: string | null
    status: CollectorStatus | string
    reasons: string[]
    collector_id: string | null
    started_at: string | null
    uptime_s: number | null
    listen: string | null
    listening: boolean | null
    sflow_listen?: string | null
    clock_policy: string | null
    rcvbuf_bytes: number | null
    socket_drops: number | null
    queue: { queue_len: number; queue_cap: number; spool_enabled: boolean; spool_records: number; spool_bytes: number; spool_dir?: string } | null
    delivery: {
      last_insert_ok_at: string | null
      last_insert_error_at: string | null
      last_insert_error?: string
      consecutive_failures: number
      stats_write_failing: boolean
      held_records: number
    } | null
    counters: (StatsCounters & { records_rejected?: number }) | null
    exporters: unknown[]
  }
  stats: {
    totals: StatsCounters & { exporters: number }
    process: {
      insert_failures: number
      insert_retries: number
      kernel_rcvbuf_errors: number
      max_queue_len: number
      queue_cap: number | null
      max_spool_records: number
      max_spool_bytes: number
      records_dropped_queue: number
      last_queue_len: number
      last_spool_records: number
      last_stats_at: string | null
      intervals: number
    } | null
    collectors: { collector_id: string; last_stats_at: string | null }[]
  } | null
  series: SeriesPoint[] | null
}

export interface ExporterStream extends StatsCounters {
  observation_domain: number
  export_protocol: string | null
  loss_pct: number | null
  clock_offset_ms: number | null
  sampling_interval: number | null
  templates_active: number
  last_datagram_at: string | null
}

export interface TemplateField {
  id: number
  enterprise: number | null
  length: number | null
  name: string
  decoded: boolean
}

export interface ExporterTemplate {
  observation_domain: number
  export_protocol: string
  template_id: number
  is_options: boolean
  fields: TemplateField[]
  flags: Record<string, boolean>
  first_seen: string | null
  last_seen: string | null
  collector_id: string | null
}

export type CapabilityStatus = 'all' | 'some' | 'none' | 'n/a' | 'unknown'

export interface Capability {
  key: string
  label: string
  status: CapabilityStatus
  pct: number | null
  used_for: string
  recommended: boolean
}

export interface ExporterFields {
  window_s: number
  records: number
  tracked: number
  legacy: number
  v6: number
  max_sampling: number | null
  min_sampling: number | null
  protocols: string[]
  versions: number[]
  direction: { ingress: number; egress: number } | null
  clock_clamped: number
  biflow_reverse: number
  capabilities: Capability[]
}

export type ExporterRole = 'edge' | 'core' | 'internal' | 'excluded'
export type CountDirection = 'auto' | 'ingress' | 'egress' | 'both'

export interface ExporterSettings {
  exporter: string
  display_name: string | null
  role: ExporterRole
  count_direction: CountDirection
  expected_interval_s: number | null
  note: string | null
  updated_at: string | null
  updated_by: string | null
  updated_by_name?: string | null
}

export interface ExporterInterface {
  ifindex: number
  rx_bytes: number
  tx_bytes: number
  if_name: string | null
  if_speed: number | null
  /** Which input the speed came from (configured circuit, NetFlow override, SNMP). */
  speed_source?: 'configured' | 'override' | 'snmp' | 'unknown'
  resolved: boolean
}

export interface ExporterHealthDetail {
  server_time: string
  window: { from: string; to: string; granularity_s: number }
  status: HealthExporterRow
  streams: ExporterStream[] | null
  series: SeriesPoint[] | null
  templates: ExporterTemplate[] | null
  fields: ExporterFields
  interfaces: ExporterInterface[] | null
  settings: ExporterSettings | null
}

export type CheckStatus = 'pass' | 'warn' | 'fail' | 'pending' | 'info' | 'na'

export interface ExporterCheck {
  id: string
  label: string
  status: CheckStatus
  detail: string
  hint: string | null
}

export interface ExporterCheckResult {
  server_time: string
  exporter: string
  /** 'stopped'/'late': flows arrived before but not within the exporter's expected interval now. */
  overall: 'ready' | 'partial' | 'waiting' | 'failing' | 'late' | 'stopped'
  status: HealthExporterRow
  checks: ExporterCheck[]
}

export interface SetupInfo {
  server_time: string
  addresses: { address: string; interface: string; family: 4 | 6; virtual: boolean }[]
  collector: {
    reachable: boolean
    status: string
    listen: string | null
    netflow_port: number
    sflow_port: number
    sflow_listening: boolean | null
    port_source: 'collector' | 'default'
  }
  recommended: {
    active_timeout_s: number
    inactive_timeout_s: number
    template_refresh_s: number
    sflow_sampling_rate: number
    sflow_polling_interval_s: number
  }
}

export interface CountingExporter {
  exporter: string
  display_name: string | null
  role: ExporterRole
  count_direction: CountDirection
  records: number
  shared_records: number
  shared_pct: number | null
  bytes: number
  shared_bytes: number
  directions: { ingress: number; egress: number; unknown: number } | null
  counted_in_edge_mode: boolean
}

export interface CountingResult {
  server_time: string
  window: { from: string; to: string }
  records: number
  flow_keys: number
  duplicate_observation_pct: number | null
  multi_point_keys: number
  multi_point_key_pct: number | null
  multi_point_records: number
  same_point_repeat_records: number
  bytes: number
  bytes_unique_estimate: number
  byte_inflation_pct: number | null
  modes: {
    all: { records: number; bytes: number }
    edge: { records: number; bytes: number; records_pct: number | null; bytes_pct: number | null; direction_known: boolean }
  }
  exporters: CountingExporter[]
  pairs: { a: string; b: string; flow_keys: number }[]
  method: string
}

export interface RetentionInfo {
  raw_days: number | null
  rollup_days: number | null
  qos_days?: number | null
  interface_days: number | null
  exporter_minutes_days: number | null
  earliest_raw: string | null
  earliest_rollup: string | null
  earliest_qos?: string | null
  earliest_interface: string | null
  earliest_exporter_minutes?: string | null
  known?: boolean
  configured?: Record<string, number | null> | null
  sampling_repair?: {
    affected?: boolean
    verified_from?: string | null
    pending_hours?: number
    unrecoverable_hours?: Record<string, number>
  } | null
}

/* ─── Exporter states ──────────────────────────────────────────────────── */

const STATE_ORDER: Record<NfExporterState, number> = { silent: 0, late: 1, never: 2, receiving: 3 }

export function stateTone(state: NfExporterState): StatusTone {
  return state === 'silent' ? 'danger' : state === 'late' ? 'warning' : state === 'receiving' ? 'success' : 'unknown'
}

/** Worst issue severity of a row (null when none). */
export function worstIssue(row: Pick<HealthExporterRow, 'issues'>): NfIssue['severity'] | null {
  const sev = (row.issues ?? []).map((i) => i.severity)
  if (sev.includes('danger')) return 'danger'
  if (sev.includes('warning')) return 'warning'
  if (sev.includes('info')) return 'info'
  return null
}

/** Rows needing attention first (silent, late, never, receiving-with-warnings), then by name. */
export function sortExporters<T extends HealthExporterRow>(rows: readonly T[]): T[] {
  const rank = (r: T) => {
    const base = STATE_ORDER[r.state] ?? 9
    const w = worstIssue(r)
    return base * 3 + (w === 'danger' ? 0 : w === 'warning' ? 1 : 2)
  }
  return [...rows].sort((a, b) => rank(a) - rank(b) || exporterLabel(a).localeCompare(exporterLabel(b), undefined, { numeric: true }))
}

export function exporterLabel(r: Pick<HealthExporterRow, 'exporter' | 'display_name'>): string {
  return r.display_name || r.exporter
}

export interface ExporterSummary {
  total: number
  receiving: number
  late: number
  silent: number
  never: number
  withProblems: number
  unmapped: number
}

export function summarizeExporters(rows: readonly HealthExporterRow[]): ExporterSummary {
  const s: ExporterSummary = { total: rows.length, receiving: 0, late: 0, silent: 0, never: 0, withProblems: 0, unmapped: 0 }
  for (const r of rows) {
    s[r.state] += 1
    const w = worstIssue(r)
    if (r.state === 'late' || r.state === 'silent' || w === 'danger' || w === 'warning') s.withProblems += 1
    if (!r.device_id) s.unmapped += 1
  }
  return s
}

/** Tone of the "exporters receiving" tile: any silent → danger, any late → warning. */
export function receivingTone(s: ExporterSummary): StatusTone {
  if (s.total === 0) return 'unknown'
  if (s.silent > 0) return 'danger'
  if (s.late > 0) return 'warning'
  if (s.receiving === 0) return 'unknown'
  return 'success'
}

export type ExporterFilterKey = 'all' | 'problems' | NfExporterState

export const EXPORTER_FILTERS: { key: ExporterFilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'problems', label: 'Needs attention' },
  { key: 'receiving', label: 'Receiving' },
  { key: 'late', label: 'Late' },
  { key: 'silent', label: 'Silent' },
  { key: 'never', label: 'Never received' },
]

export function filterExporters<T extends HealthExporterRow>(rows: readonly T[], key: ExporterFilterKey, query = ''): T[] {
  const q = query.trim().toLowerCase()
  return rows.filter((r) => {
    if (key === 'problems') {
      const w = worstIssue(r)
      if (!(r.state === 'late' || r.state === 'silent' || w === 'danger' || w === 'warning')) return false
    } else if (key !== 'all' && r.state !== key) return false
    if (!q) return true
    return [r.exporter, r.display_name, r.device_hostname].some((v) => (v ?? '').toLowerCase().includes(q))
  })
}

/**
 * Receipt age now, from the server-computed age at `server_time` plus the time elapsed since (on
 * the appliance clock via `serverNow`). Never from the browser clock (NF-01).
 */
export function currentAgeSeconds(row: Pick<HealthExporterRow, 'receipt_age_s'>, serverTime: string | null | undefined, nowMs: number): number | null {
  if (row.receipt_age_s == null) return null
  const at = serverTime ? Date.parse(serverTime) : NaN
  const elapsed = Number.isFinite(at) ? Math.max(0, (nowMs - at) / 1000) : 0
  return row.receipt_age_s + elapsed
}

/** "12 s", "4 min", "2.5 h", "3 d" — compact age for tables. */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 90) return `${Math.round(seconds)} s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  if (seconds < 172_800) return `${trim(seconds / 3600)} h`
  return `${trim(seconds / 86_400)} d`
}

function trim(v: number): string {
  return String(Number(v < 10 ? v.toFixed(1) : v.toFixed(0)))
}

/** Clock offset in words: "3 min ahead", "240 ms behind", "in sync". */
export function formatClockOffset(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  const abs = Math.abs(ms)
  if (abs < 1000) return abs < 50 ? 'in sync' : `${Math.round(abs)} ms ${ms < 0 ? 'ahead' : 'behind'}`
  return `${formatAge(abs / 1000)} ${ms < 0 ? 'ahead' : 'behind'}`
}

export function clockTone(ms: number | null | undefined): StatusTone {
  if (ms == null) return 'unknown'
  const abs = Math.abs(ms)
  if (abs <= 5_000) return 'success'
  if (abs < 60_000) return 'warning'
  return 'danger'
}

export const PROTOCOL_LABELS: Record<string, string> = {
  netflow5: 'NetFlow v5',
  netflow9: 'NetFlow v9',
  ipfix: 'IPFIX',
  sflow5: 'sFlow v5',
}

export function protocolList(protocols: readonly string[] | null | undefined): string {
  if (!protocols || protocols.length === 0) return '—'
  return protocols.map((p) => PROTOCOL_LABELS[p] ?? p).join(', ')
}

export function samplingLabel(n: number | null | undefined): string {
  if (n == null) return '—'
  return n <= 1 ? 'Unsampled' : `1 in ${n.toLocaleString('en-US')}`
}

export function intervalBasisLabel(basis: HealthExporterRow['interval_basis']): string {
  return basis === 'configured' ? 'configured' : basis === 'learned' ? 'learned from its cadence' : 'default'
}

/* ─── Collector ────────────────────────────────────────────────────────── */

/**
 * The Collector tile's caption (NF-03): loading while the health query is pending, "could not be
 * loaded" when the request failed, and "did not answer" only when the API reached no collector
 * (`live.reachable === false`), never for a request that is still running.
 */
export function collectorCaption(
  status: 'pending' | 'error' | 'success',
  live: { reachable?: boolean; listen?: string | null; reasons?: string[] | null } | null | undefined,
): string | undefined {
  if (live?.reasons?.[0]) return live.reasons[0]
  if (live?.reachable) return `Listening on UDP ${live.listen?.replace(/^.*:/, '') ?? '—'}`
  if (live && live.reachable === false) return 'The collector health endpoint did not answer'
  if (status === 'error') return 'Health could not be loaded'
  if (status === 'pending') return 'Checking the collector…'
  return undefined
}

export function collectorTone(status: string | null | undefined, reachable: boolean): StatusTone {
  if (!reachable) return 'unknown'
  if (status === 'ok') return 'success'
  if (status === 'degraded') return 'warning'
  if (status === 'down') return 'danger'
  return 'unknown'
}

export function collectorWord(status: string | null | undefined, reachable: boolean): string {
  if (!reachable) return 'Unknown'
  return status === 'ok' ? 'Healthy' : status === 'degraded' ? 'Degraded' : status === 'down' ? 'Down' : 'Unknown'
}

/** Records the collector could not use (every drop reason) over a totals block. */
export function droppedRecords(t: Partial<StatsCounters> | null | undefined): number {
  if (!t) return 0
  return (t.records_dropped_implausible ?? 0) + (t.records_dropped_queue ?? 0) + (t.records_dropped_unknown_exporter ?? 0) + (t.records_unsupported ?? 0)
}

export interface FilledPoint extends Partial<SeriesPoint> {
  ts: string
  t: number
  /** No statistics row for the bucket: the collector wrote nothing (stopped, or before it existed). */
  missing: boolean
}

/**
 * A continuous bucket grid for [from, to) at `granularityS`: every bucket appears, and buckets
 * without a statistics row are `missing` (drawn as a gap, never as zero).
 */
export function fillSeries(series: readonly SeriesPoint[] | null | undefined, from: string, to: string, granularityS: number): FilledPoint[] {
  const g = Math.max(1, granularityS) * 1000
  const start = Math.floor(Date.parse(from) / g) * g
  const end = Date.parse(to)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return []
  const byTs = new Map<number, SeriesPoint>()
  for (const p of series ?? []) {
    const t = Date.parse(p.ts)
    if (Number.isFinite(t)) byTs.set(Math.floor(t / g) * g, p)
  }
  const out: FilledPoint[] = []
  for (let t = start; t < end && out.length < 2000; t += g) {
    const p = byTs.get(t)
    out.push(p ? { ...p, t, missing: false } : { ts: new Date(t).toISOString(), t, missing: true })
  }
  return out
}

/* ─── Onboarding checks ────────────────────────────────────────────────── */

export function checkTone(status: CheckStatus): StatusTone {
  switch (status) {
    case 'pass':
      return 'success'
    case 'warn':
      return 'warning'
    case 'fail':
      return 'danger'
    case 'info':
      return 'info'
    default:
      return 'unknown'
  }
}

export const OVERALL_TEXT: Record<ExporterCheckResult['overall'], { word: string; tone: StatusTone; text: string }> = {
  ready: { word: 'Ready', tone: 'success', text: 'Flows arrive and carry every recommended field.' },
  partial: { word: 'Working, with gaps', tone: 'warning', text: 'Flows arrive; some recommended settings or fields are missing.' },
  waiting: { word: 'Waiting', tone: 'info', text: 'Nothing has arrived from this address yet. This page checks again every few seconds.' },
  failing: { word: 'Action needed', tone: 'danger', text: 'Flows are arriving but cannot be used as configured.' },
  late: { word: 'Late', tone: 'warning', text: 'Flows arrived before, but none within the expected interval. This page checks again every few seconds.' },
  stopped: { word: 'Not receiving', tone: 'danger', text: 'Flows arrived from this address before, but nothing is arriving now. See "Datagrams received" for when it stopped.' },
}

/** Live-check poll interval: quick while waiting for (or missing) flows, slower once they arrive. */
export function checkPollMs(res: Pick<ExporterCheckResult, 'overall'> | null | undefined): number {
  return !res || res.overall === 'waiting' || res.overall === 'late' || res.overall === 'stopped' ? 5_000 : 15_000
}

/**
 * The appliance address to pre-fill: the address the browser reached it on when that is one of
 * its own (not loopback), else its first physical IPv4, else any address. Empty until the
 * appliance's addresses are known, so a loopback page URL is never offered as the destination.
 */
export function preferredCollectorAddress(addresses: SetupInfo['addresses'], host: string): string {
  const usable = (a: string) => isIpAddress(a) && !/^127\./.test(a) && a !== '::1'
  if (usable(host) && addresses.some((a) => a.address === host)) return host
  return addresses.find((a) => !a.virtual && a.family === 4)?.address
    ?? addresses.find((a) => !a.virtual)?.address
    ?? addresses[0]?.address
    ?? (usable(host) ? host : '')
}

/* ─── Counting ─────────────────────────────────────────────────────────── */

export const ROLE_LABELS: Record<ExporterRole, string> = {
  edge: 'Edge (counted)',
  core: 'Core (not counted in edge mode)',
  internal: 'Internal (not counted in edge mode)',
  excluded: 'Excluded (not counted in edge mode)',
}

export const DIRECTION_LABELS: Record<CountDirection, string> = {
  auto: 'Automatic',
  ingress: 'Ingress records only',
  egress: 'Egress records only',
  both: 'Both directions',
}

/** One sentence stating how much traffic is seen at more than one collection point. */
export function duplicationSentence(c: Pick<CountingResult, 'records' | 'duplicate_observation_pct' | 'multi_point_key_pct' | 'byte_inflation_pct'>): string {
  if (!c.records) return 'No flow records arrived in the measured window, so duplication cannot be estimated.'
  const dup = c.duplicate_observation_pct ?? 0
  if (dup < 1) return 'Almost every flow is reported by a single collection point: totals are not inflated by duplicate observations.'
  const infl = c.byte_inflation_pct
  return (
    `About ${fmtPct(dup)} of flow records are additional observations of flows another record already reported` +
    (c.multi_point_key_pct != null ? ` (${fmtPct(c.multi_point_key_pct)} of flows are seen at more than one exporter)` : '') +
    (infl != null && infl >= 1 ? `; summing every observation overstates volume by about ${fmtPct(infl)}.` : '.')
  )
}

function fmtPct(v: number): string {
  return `${Number(v.toFixed(v < 10 ? 1 : 0))}%`
}

/* ─── Retention (FE-11) ────────────────────────────────────────────────── */

export interface RetentionRow {
  key: string
  dataset: string
  granularity: string
  days: number | null
  configured: number | null
  earliest: string | null
  powers: string
}

export function retentionRows(r: RetentionInfo): RetentionRow[] {
  const conf = r.configured ?? {}
  return [
    { key: 'raw', dataset: 'Raw flow records', granularity: 'Each flow', days: r.raw_days, configured: conf.raw_days ?? null, earliest: r.earliest_raw, powers: 'Forensics search and export, every filter, short windows' },
    { key: 'rollup', dataset: 'Hourly conversations', granularity: '1 hour', days: r.rollup_days, configured: conf.rollup_days ?? null, earliest: r.earliest_rollup, powers: 'Long overview windows, top talkers and conversations beyond raw retention' },
    { key: 'qos', dataset: 'Hourly QoS / TCP flags', granularity: '1 hour', days: r.qos_days ?? null, configured: conf.qos_days ?? null, earliest: r.earliest_qos ?? null, powers: 'DSCP and TCP-flag views on long windows' },
    { key: 'interface', dataset: 'Interface traffic', granularity: '5 minutes', days: r.interface_days, configured: conf.interface_days ?? null, earliest: r.earliest_interface, powers: 'Capacity, trends and forecasts, interface views' },
    { key: 'minutes', dataset: 'Exporter receipts', granularity: '1 minute', days: r.exporter_minutes_days, configured: conf.exporter_minutes_days ?? null, earliest: r.earliest_exporter_minutes ?? null, powers: 'Coverage and exporter health' },
  ]
}

export function retentionText(days: number | null | undefined): string {
  if (days == null) return 'No limit'
  return `${days} days`
}

/* ─── Exporter mapping (FE-08) ─────────────────────────────────────────── */

export interface ExporterDeviceRow {
  exporter_ip: string
  display_name?: string | null
  flows: number
  last_seen: string | null
  device_id: string | null
  device_hostname: string | null
  source: 'mapped' | 'ip-match' | null
  overrides_device_id?: string | null
  overrides_device_hostname?: string | null
  note?: string | null
}

export interface DeviceFacts {
  device_id: string
  hostname: string
  ip_address: string | null
  vendor: string | null
  model: string | null
  status: string | null
  device_type?: string | null
  snmp_enabled: boolean
  tags: string[]
  interfaces: number
  interfaces_with_speed: number
}

export interface Guidance {
  code: string
  message: string
}

export interface SuggestionEvidence {
  kind: string
  score: number
  text: string
  line?: number
  seen_at?: string | null
  captured_at?: string | null
}

export interface SuggestionCandidate extends DeviceFacts {
  score: number
  confidence: 'high' | 'medium' | 'low'
  evidence: SuggestionEvidence[]
  current_owner: boolean
  guidance: Guidance[]
}

export interface Suggestions {
  exporter: string
  reverse_dns: string | null
  current_owner: { device_id: string; hostname: string; source: 'mapped' | 'ip-match' } | null
  candidates: SuggestionCandidate[]
  sources: {
    ncm: { available: boolean; permitted: boolean; scanned: number; complete: boolean }
    udt: boolean
    reverse_dns: boolean
    /** 'disabled' when reverse DNS is turned off in NetFlow settings (Enrichment): no lookup was sent. */
    reverse_dns_status?: 'disabled' | 'ok' | 'no_record' | 'pending'
  }
}

/** How the suggestions describe the reverse-DNS evidence (FE-09: off means no lookup). */
export function reverseDnsSourceText(s: Pick<Suggestions, 'reverse_dns' | 'sources'>): string {
  switch (s.sources.reverse_dns_status) {
    case 'disabled':
      return 'reverse DNS (turned off in NetFlow settings, not looked up)'
    case 'pending':
      return 'reverse DNS (no answer in time)'
    default:
      return s.reverse_dns ? `reverse DNS (${s.reverse_dns})` : 'reverse DNS (no name)'
  }
}

export interface MappingImpact {
  exporter: string
  current_owner: { device_id: string; hostname: string; source: 'mapped' | 'ip-match'; tags: string[] } | null
  target: DeviceFacts
  unchanged: boolean
  overrides_ip_match: { device_id: string; hostname: string } | null
  interfaces: {
    seen_24h: number
    resolve_on_target: number
    with_speed_on_target: number
    resolve_on_current: number | null
    unresolved_sample: number[]
  }
  visibility: { current_tags: string[]; target_tags: string[]; note: string }
  guidance: Guidance[]
}

export interface HistoryEntry {
  at: string
  actor: string | null
  action: string
  kind: 'mapping' | 'settings'
  details: Record<string, unknown>
}

/** Plain-language lines describing what a link changes (the confirmation text). */
export function impactLines(i: MappingImpact): string[] {
  const out: string[] = []
  const t = i.target
  const cur = i.current_owner
  if (i.unchanged) return [`${i.exporter} is already linked to ${t.hostname}.`]
  out.push(
    cur
      ? `Interface names, speeds and ownership move from ${cur.hostname} (${cur.source === 'mapped' ? 'linked' : 'management-IP match'}) to ${t.hostname}.`
      : `Interface names, speeds and ownership of ${i.exporter} will come from ${t.hostname}.`,
  )
  const s = i.interfaces
  if (s.seen_24h > 0) {
    out.push(
      `${s.resolve_on_target} of the ${s.seen_24h} interface indexes this exporter reported in the last 24 hours exist on ${t.hostname}` +
        (s.resolve_on_target ? `, ${s.with_speed_on_target} with a known speed.` : '.'),
    )
  } else {
    out.push('The exporter reported no interface indexes in the last 24 hours, so names cannot be checked yet.')
  }
  const gained = t.tags.filter((x) => !(i.visibility.current_tags ?? []).includes(x))
  const lost = (i.visibility.current_tags ?? []).filter((x) => !t.tags.includes(x))
  if (gained.length || lost.length) {
    const parts: string[] = []
    if (gained.length) parts.push(`users limited to ${gained.join(', ')} will see its flows`)
    if (lost.length) parts.push(`users limited only to ${lost.join(', ')} will no longer see them`)
    out.push(`Visibility: ${parts.join('; ')}.`)
  } else if (!t.tags.length) {
    out.push(`${t.hostname} has no tags: only unrestricted users will see this exporter's flows.`)
  }
  return out
}
