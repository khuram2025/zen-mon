/**
 * Detection findings: response shapes and the pure logic behind the Anomalies page and the
 * Network & Security dashboard (B5; NF-03, NF-07, NF-09, FE-06, CR-10, CR-11).
 *
 * Kept free of React so `scripts/test-netflow-anomalies.mjs` can test it directly.
 */
import { flowFilterFromDraft, type FlowFilter, type FlowFilterDraft } from '@/lib/netflow/filters'
import { isIpAddress } from '@/lib/netflow/ip'
import { buildNetflowLink, hostPath } from '@/lib/netflow/links'
import type { StatusTone } from '@/components/netflow/StatusTile'

export type Severity = 'critical' | 'warning' | 'info'
export type FindingStatus = 'open' | 'acknowledged' | 'suppressed' | 'resolved' | 'candidate'
export type DetectorState = 'ok' | 'unavailable' | 'no_coverage' | 'failed' | 'disabled'
export type Confidence = 'high' | 'medium' | 'low'

export interface TimeSpan {
  from: string
  to: string
}

export interface FindingSegment extends TimeSpan {
  peak?: number | null
}

export interface FindingEvidence {
  /** Frozen investigation window: first occurrence → last occurrence (≤ 31 days). */
  window?: TimeSpan | null
  /** The latest occurrence. */
  latest?: TimeSpan | null
  /** Detector predicates in canonical filter keys. */
  filter?: Record<string, string> | null
  /** The host a broader "all traffic" investigation should follow. */
  host?: string | null
  segments?: FindingSegment[] | null
  truncated_span?: boolean
}

export interface UserRef {
  id: string
  username: string | null
}

export interface FindingNote {
  id: string
  at: string
  by: string | null
  by_name: string | null
  kind: 'note' | 'action' | 'system'
  text: string
}

export interface Finding {
  id: string
  fingerprint: string
  persisted_id?: string | null
  persisted_status?: FindingStatus | null
  kind: string
  label: string
  category: string | null
  severity: Severity
  confidence: Confidence | null
  status: FindingStatus
  title: string
  description: string
  src: string | null
  dst: string | null
  exporter: string | null
  host: string | null
  first_seen: string | null
  last_seen: string | null
  occurrences: number | null
  evidence: FindingEvidence | null
  metrics: Record<string, unknown> | null
  method: string | null
  threshold: Record<string, unknown> | null
  observed: Record<string, unknown> | null
  baseline: Record<string, unknown> | null
  suppressed: boolean
  suppression: { reason: string | null; until: string | null; by: UserRef | null; exception_id?: string | null } | null
  assigned_to: UserRef | null
  acknowledged_by: UserRef | null
  acknowledged_at: string | null
  resolved_at: string | null
  notes_count: number
  persisted: boolean
  notes?: FindingNote[]
  permissions?: { acknowledge: boolean; suppress: boolean }
  detector?: { kind: string; label: string; description: string; method: string; required_fields: string[] } | null
}

export interface DetectorStatusRow {
  kind: string
  label: string
  category: string
  status: DetectorState
  reason: string | null
  last_run_at: string | null
  last_ok_at?: string | null
  coverage_pct?: number | null
  exporters_unavailable?: string[]
  reduced_sensitivity?: { exporter: string; sampling: number }[]
  notes?: string[]
  required_fields?: string[]
  enabled?: boolean
  match_count?: number
  returned?: number
  truncated?: boolean
  error_id?: string | null
}

export interface EngineStatus {
  status: 'running' | 'stale' | 'never'
  last_run_at: string | null
  last_bucket_end: string | null
  age_s: number | null
  evaluated_pct?: number | null
  gaps?: [string, string][]
  bucket_s?: number
  grace_s?: number
  last_error?: { at: string; bucket: string } | null
}

export interface FindingCounts {
  open: number
  acknowledged: number
  suppressed: number
  resolved: number
  active: number
  /** Active (open + acknowledged) findings per severity. */
  by_severity: Record<Severity, number>
  /** Open findings per severity. */
  by_severity_open?: Record<Severity, number>
  /** Active findings per detector, within the detector filter. */
  by_kind: Record<string, number>
  /** Active findings per detector, ignoring the detector filter (for the picker). */
  by_kind_all?: Record<string, number>
}

/** `GET /netflow/findings` (contract §11). */
export interface FindingsResponse {
  items: Finding[]
  total: number
  offset: number
  limit: number
  counts: FindingCounts
  detectors: DetectorStatusRow[]
  engine: EngineStatus
  complete: boolean
  window: TimeSpan
  quiet_minutes: number
}

/** `GET /netflow/findings/run` (ad-hoc evaluation over raw records). */
export interface RunResponse {
  items: Finding[]
  counts: { candidate: number; suppressed: number; by_severity: Record<Severity, number>; matches: number; truncated: boolean }
  detectors: DetectorStatusRow[]
  coverage: { records: number; tcp_records: number; tcp_flagged_records: number; flag_coverage_pct: number | null } | null
  complete: boolean
  window: TimeSpan
  generated_at: string
}

export interface TimelinePoint {
  t: string
  /** false: the exporter delivered nothing in this bucket (values are null = unknown, not zero). */
  covered?: boolean | null
  records: number | null
  bytes: number | null
  packets: number | null
  pps: number | null
  bps: number | null
  dsts: number | null
  srcs: number | null
  dports: number | null
  rsts: number | null
}

/** Chart value of a timeline point: null where nothing was received (a gap, never a zero bar). */
export function timelineValue(p: TimelinePoint, key: keyof TimelinePoint): number | null {
  if (p.covered === false) return null
  const v = p[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export interface TimelineResponse {
  finding_id: string
  window: TimeSpan
  bucket_s: number
  filter: Record<string, string>
  points: TimelinePoint[]
  segments: FindingSegment[]
  metric: { key: keyof TimelinePoint; label: string }
  threshold: Record<string, unknown> | null
}

/* ─── Labels and tones ─────────────────────────────────────────────── */

export const SEVERITIES: Severity[] = ['critical', 'warning', 'info']

export const SEVERITY_TONE: Record<Severity, StatusTone> = { critical: 'danger', warning: 'warning', info: 'info' }
export const SEVERITY_LABEL: Record<Severity, string> = { critical: 'Critical', warning: 'Warning', info: 'Info' }

export const STATUS_LABEL: Record<FindingStatus, string> = {
  open: 'Open',
  acknowledged: 'Acknowledged',
  suppressed: 'Suppressed',
  resolved: 'Resolved',
  candidate: 'Candidate',
}

export const DETECTOR_STATE: Record<DetectorState, { label: string; tone: StatusTone; hint: string }> = {
  ok: { label: 'Ran', tone: 'success', hint: 'Evaluated with data' },
  unavailable: { label: 'Unavailable', tone: 'warning', hint: 'The exporters do not send the fields it needs' },
  no_coverage: { label: 'No data', tone: 'unknown', hint: 'Nothing to evaluate in this period' },
  failed: { label: 'Failed', tone: 'danger', hint: 'The check could not run' },
  disabled: { label: 'Off', tone: 'neutral', hint: 'Turned off in detection settings' },
}

export const CONFIDENCE_LABEL: Record<Confidence, string> = { high: 'High confidence', medium: 'Medium confidence', low: 'Low confidence' }

export const FIELD_LABEL: Record<string, string> = {
  tcp_flags: 'TCP flags',
  icmp_type: 'ICMP type',
  baseline: 'Baseline history',
  ti_feeds: 'Threat-intel feeds',
}

export const STATUS_FILTERS = ['active', 'open', 'acknowledged', 'suppressed', 'resolved', 'all'] as const
export type StatusFilter = (typeof STATUS_FILTERS)[number]
export const STATUS_FILTER_LABEL: Record<StatusFilter, string> = {
  active: 'Active',
  open: 'Open',
  acknowledged: 'Acknowledged',
  suppressed: 'Suppressed',
  resolved: 'Resolved',
  all: 'All',
}

export function isStatusFilter(v: string | null | undefined): v is StatusFilter {
  return !!v && (STATUS_FILTERS as readonly string[]).includes(v)
}

/** The detector registry's kinds (`services/netflow_detection.py` DETECTORS). */
export const DETECTOR_KINDS = [
  'syn_scan',
  'port_scan',
  'rst_flood',
  'sensitive_egress',
  'icmp_flood',
  'icmp_flood_target',
  'icmp_sweep',
  'dominant_sender',
  'volume_spike',
  'threat_intel',
] as const
export type DetectorKind = (typeof DETECTOR_KINDS)[number]

export function isDetectorKind(v: string | null | undefined): v is DetectorKind {
  return !!v && (DETECTOR_KINDS as readonly string[]).includes(v)
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** `?finding=<id>` of a link to one finding (alerts, host profile); null unless a UUID. */
export function findingFocusId(sp: URLSearchParams): string | null {
  const v = (sp.get('finding') ?? '').trim()
  return UUID_RE.test(v) ? v.toLowerCase() : null
}

export function isSeverity(v: string | null | undefined): v is Severity {
  return v === 'critical' || v === 'warning' || v === 'info'
}

/* ─── Detector coverage and the assessment sentence (NF-03) ────────── */

export interface DetectorSummary {
  total: number
  enabled: number
  ran: DetectorStatusRow[]
  unavailable: DetectorStatusRow[]
  noData: DetectorStatusRow[]
  failed: DetectorStatusRow[]
  disabled: DetectorStatusRow[]
  /** Ran, but not on every exporter (fields missing on some, or sampling). */
  partial: DetectorStatusRow[]
}

/**
 * Every exporter a detector ran on with reduced sensitivity because of sampling (X-09), most
 * heavily sampled first. The API note names only the first few ("… and N more"), so the coverage
 * list renders this instead of that note.
 */
export function reducedSensitivityText(rows: DetectorStatusRow['reduced_sensitivity'] | null | undefined): string | null {
  if (!rows?.length) return null
  const sorted = [...rows].sort((a, b) => b.sampling - a.sampling || a.exporter.localeCompare(b.exporter))
  return (
    `Reduced sensitivity on ${sorted.length} sampled exporter${sorted.length === 1 ? '' : 's'}: ` +
    `${sorted.map((r) => `${r.exporter} 1:${r.sampling}`).join(', ')}. Record-count thresholds see only the sampled share of the traffic.`
  )
}

/** A detector's notes minus the (shortened) sampling note when the full list is rendered instead. */
export function detectorNotes(d: Pick<DetectorStatusRow, 'notes' | 'reduced_sensitivity'>): string[] {
  const notes = d.notes ?? []
  if (!d.reduced_sensitivity?.length) return notes
  return notes.filter((n) => !n.startsWith('Reduced sensitivity on sampled exporters'))
}

export function summarizeDetectors(rows: DetectorStatusRow[] | null | undefined): DetectorSummary {
  const list = rows ?? []
  const by = (s: DetectorState) => list.filter((r) => r.status === s)
  const ran = by('ok')
  return {
    total: list.length,
    enabled: list.filter((r) => r.status !== 'disabled').length,
    ran,
    unavailable: by('unavailable'),
    noData: by('no_coverage'),
    failed: by('failed'),
    disabled: by('disabled'),
    partial: ran.filter((r) => (r.exporters_unavailable?.length ?? 0) > 0 || (r.reduced_sensitivity?.length ?? 0) > 0),
  }
}

export interface Assessment {
  tone: StatusTone
  title: string
  detail: string
  /** Detectors that did not run, for listing next to the sentence. */
  notRun: DetectorStatusRow[]
}

function names(rows: DetectorStatusRow[], n = 3): string {
  const labels = rows.map((r) => r.label)
  return labels.length <= n ? labels.join(', ') : `${labels.slice(0, n).join(', ')} and ${labels.length - n} more`
}

/**
 * What an empty findings list means. It is never "all checks passed": at best the detectors that
 * ran found no candidates, and the sentence names the ones that did not run (NF-03, X-33).
 */
export function assessEmpty(opts: {
  detectors: DetectorStatusRow[] | null | undefined
  complete: boolean
  engine?: EngineStatus | null
  mode: 'stored' | 'run'
  evaluatedPct?: number | null
}): Assessment {
  const s = summarizeDetectors(opts.detectors)
  const notRun = [...s.failed, ...s.unavailable, ...s.noData]
  if (opts.mode === 'stored' && opts.engine?.status === 'never') {
    return {
      tone: 'unknown',
      title: 'The detection engine has not evaluated any traffic yet',
      detail: 'It evaluates each 5-minute period two minutes after it ends. Run the detectors over this window instead to check it now.',
      notRun,
    }
  }
  if (opts.mode === 'stored' && opts.engine?.status === 'stale') {
    return {
      tone: 'danger',
      title: 'The detection engine is not running',
      detail: `It last evaluated traffic ${opts.engine.last_run_at ? `at ${opts.engine.last_run_at}` : 'a while ago'}; findings after that are missing, not absent.`,
      notRun,
    }
  }
  if (s.ran.length === 0) {
    return {
      tone: 'warning',
      title: 'No detector could evaluate this window',
      detail: notRun.length ? `${names(notRun)}: ${notRun[0].reason ?? DETECTOR_STATE[notRun[0].status].hint}` : 'Every detector is turned off.',
      notRun,
    }
  }
  const partialWindow = opts.mode === 'stored' && typeof opts.evaluatedPct === 'number' && opts.evaluatedPct < 99
  if (opts.complete && !partialWindow && s.partial.length === 0) {
    return {
      tone: 'success',
      title: `No findings from the ${s.ran.length} detector${s.ran.length === 1 ? '' : 's'} that ran`,
      detail: 'These are heuristics over flow records, not a security guarantee: they only cover the patterns listed under detector coverage.',
      notRun,
    }
  }
  const parts: string[] = []
  if (notRun.length) parts.push(`Not evaluated: ${names(notRun)}.`)
  if (s.partial.length) parts.push(`${names(s.partial)} ran on part of the traffic only.`)
  if (partialWindow) parts.push(`The engine evaluated ${opts.evaluatedPct}% of this window.`)
  return {
    tone: 'warning',
    title: `No findings from the ${s.ran.length} of ${s.enabled} detectors that ran`,
    detail: parts.join(' ') || 'Some detectors did not run on this window.',
    notRun,
  }
}

/** One-line coverage label ("8 of 10 detectors ran · 2 unavailable"). */
export function coverageLine(rows: DetectorStatusRow[] | null | undefined): string {
  const s = summarizeDetectors(rows)
  if (!s.total) return 'Detector status unknown'
  const bits = [`${s.ran.length} of ${s.enabled} detector${s.enabled === 1 ? '' : 's'} ran`]
  if (s.unavailable.length) bits.push(`${s.unavailable.length} unavailable`)
  if (s.noData.length) bits.push(`${s.noData.length} without data`)
  if (s.failed.length) bits.push(`${s.failed.length} failed`)
  if (s.disabled.length) bits.push(`${s.disabled.length} off`)
  return bits.join(' · ')
}

/* ─── Links (NF-09) ────────────────────────────────────────────────── */

/** The detector predicates as a typed flow filter (invalid values dropped). */
export function evidenceFilter(f: Pick<Finding, 'evidence'>): FlowFilter {
  const raw = (f.evidence?.filter ?? {}) as FlowFilterDraft
  const draft: FlowFilterDraft = {}
  for (const [k, v] of Object.entries(raw)) {
    if (v !== null && v !== undefined && String(v) !== '') (draft as Record<string, string>)[k] = String(v)
  }
  return flowFilterFromDraft(draft).filter
}

/** The frozen evidence window, falling back to first/last seen. */
export function evidenceWindow(f: Pick<Finding, 'evidence' | 'first_seen' | 'last_seen'>): TimeSpan | null {
  const w = f.evidence?.window
  if (w?.from && w?.to && Date.parse(w.from) < Date.parse(w.to)) return { from: w.from, to: w.to }
  if (f.first_seen && f.last_seen && Date.parse(f.first_seen) < Date.parse(f.last_seen)) return { from: f.first_seen, to: f.last_seen }
  return null
}

/**
 * Investigate: Forensics with the detector's own predicates over the frozen evidence window, plus
 * the finding id so Forensics can explain what it is showing. Never a live relative window: the
 * finding's time segment is what was judged.
 */
export function investigateLink(f: Finding): string | null {
  const win = evidenceWindow(f)
  if (!win) return null
  return buildNetflowLink('/netflow/forensics', {
    filter: evidenceFilter(f),
    window: { mode: 'fixed', from: win.from, to: win.to },
    extra: { finding: f.persisted ? f.id : f.persisted_id ?? undefined, finding_kind: f.kind, run: true },
  })
}

/** The latest occurrence only (a precise, small window). */
export function latestLink(f: Finding): string | null {
  const l = f.evidence?.latest
  if (!l?.from || !l?.to) return null
  return buildNetflowLink('/netflow/forensics', {
    filter: evidenceFilter(f),
    window: { mode: 'fixed', from: l.from, to: l.to },
    extra: { finding: f.persisted ? f.id : f.persisted_id ?? undefined, finding_kind: f.kind, run: true },
  })
}

/** "All traffic for this host": any protocol, any peer, same frozen window. */
export function hostTrafficLink(f: Finding): string | null {
  const win = evidenceWindow(f)
  const host = f.host ?? f.src ?? f.dst
  if (!win || !host) return null
  return buildNetflowLink('/netflow/forensics', {
    filter: { talker: host },
    window: { mode: 'fixed', from: win.from, to: win.to },
    extra: { run: true },
  })
}

export function hostProfileLink(f: Finding): string | null {
  const host = f.host ?? f.src ?? f.dst
  return host ? hostPath(host) : null
}

/**
 * A NetFlow page link for a team-dashboard range (1h/6h/24h/7d): the NetFlow pages' own presets
 * where they exist, otherwise the range as fixed bounds ending now (X-44: the page opened must show
 * the same period as the tile that was clicked).
 */
export function rangeLink(path: string, rangeKey: string, hours: number, nowMs: number, extra?: Record<string, string>): string {
  const presets = new Set(['1h', '24h', '7d'])
  if (presets.has(rangeKey)) return buildNetflowLink(path, { extra: { range: rangeKey, ...extra } })
  const to = new Date(Math.floor(nowMs / 60_000) * 60_000)
  const from = new Date(to.getTime() - hours * 3_600_000)
  return buildNetflowLink(path, {
    window: { mode: 'fixed', from: from.toISOString(), to: to.toISOString() },
    extra,
  })
}

export function anomaliesLinkForRange(rangeKey: string, hours: number, nowMs: number, extra?: Record<string, string>): string {
  return rangeLink('/netflow/anomalies', rangeKey, hours, nowMs, extra)
}

/* ─── Presentation helpers ─────────────────────────────────────────── */

export function subjectText(f: Pick<Finding, 'src' | 'dst' | 'host'>): string {
  if (f.src && f.dst) return `${f.src} → ${f.dst}`
  if (f.src) return `${f.src} →`
  if (f.dst) return `→ ${f.dst}`
  return f.host ?? '—'
}

const THRESHOLD_TEXT: Record<string, (v: unknown) => string> = {
  destinations_per_5min: (v) => `≥ ${fmt(v)} destinations per 5 min`,
  ports_per_5min: (v) => `≥ ${fmt(v)} ports per 5 min`,
  critical_at: (v) => `critical at ${fmt(v)}`,
  min_flows_per_5min: (v) => `≥ ${fmt(v)} flagged TCP records per 5 min`,
  min_ratio_pct: (v) => `RST share ≥ ${fmt(v)}%`,
  ports: (v) => `ports ${Array.isArray(v) ? v.join(', ') : String(v)}`,
  min_bytes_without_ack: (v) => `≥ ${fmt(v)} bytes when no ACK was seen`,
  min_pps: (v) => `≥ ${fmt(v)} packets/s`,
  effective_pps: (v) => `effective ${fmt(v)} packets/s (incl. baseline)`,
  min_minutes: (v) => `for ${fmt(v)} consecutive minutes`,
  baseline_multiplier: (v) => `above ${fmt(v)}× own baseline`,
  min_share_pct: (v) => `share ≥ ${fmt(v)}%`,
  min_rate_mbps: (v) => `rate ≥ ${fmt(v)} Mbps`,
  multiplier: (v) => `≥ ${fmt(v)}× baseline`,
  min_baseline_hours: (v) => `with ≥ ${fmt(v)} h of history`,
  feeds: (v) => `${fmt(v)} enabled feed${Number(v) === 1 ? '' : 's'}`,
  include_inbound: (v) => (v ? 'inbound-only traffic reported' : 'outbound traffic only'),
}

function fmt(v: unknown): string {
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(Math.round(v * 100) / 100)
  return String(v)
}

/** The thresholds a finding was judged against, as short phrases. */
export function thresholdPhrases(threshold: Record<string, unknown> | null | undefined): string[] {
  if (!threshold) return []
  return Object.entries(threshold)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => (THRESHOLD_TEXT[k] ? THRESHOLD_TEXT[k](v) : `${k.replace(/_/g, ' ')}: ${fmt(v)}`))
}

const OBSERVED_LABEL: Record<string, string> = {
  peak_destinations: 'Peak destinations / 5 min',
  latest_destinations: 'Latest destinations / 5 min',
  peak_ports: 'Peak ports',
  sample_ports: 'Ports seen',
  latest_ports: 'Latest ports / 5 min',
  port_low: 'Lowest port',
  port_high: 'Highest port',
  rst_records: 'Records with RST',
  flagged_tcp_records: 'Flagged TCP records',
  ratio_pct: 'RST share (%)',
  peak_ratio_pct: 'Peak RST share (%)',
  ports: 'Ports',
  bytes: 'Bytes',
  sent_bytes: 'Bytes sent to it',
  records: 'Records',
  acked_records: 'Records with ACK',
  peak_pps: 'Peak packets/s',
  sustained_minutes: 'Longest sustained run (min)',
  packets: 'Packets',
  peers: 'Peers',
  runs: 'Separate bursts',
  share_pct: 'Share of exporter traffic (%)',
  peak_share_pct: 'Peak share (%)',
  total_bytes: 'Exporter total bytes',
  rate_bps: 'Average rate (bps)',
  peak_rate_bps: 'Peak rate (bps)',
  ratio: '× baseline',
  direction: 'Direction',
  internal_hosts: 'Internal hosts',
}

export interface LabeledValue {
  key: string
  label: string
  value: unknown
}

export function observedPairs(observed: Record<string, unknown> | null | undefined): LabeledValue[] {
  if (!observed) return []
  return Object.entries(observed)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => ({ key: k, label: OBSERVED_LABEL[k] ?? k.replace(/_/g, ' '), value: v }))
}

/** Lifecycle actions offered for a status and the caller's permissions (server re-checks). */
export type LifecycleAction = 'acknowledge' | 'assign' | 'suppress' | 'unsuppress' | 'resolve' | 'reopen' | 'note'

export function allowedActions(
  status: FindingStatus,
  perms: { acknowledge: boolean; suppress: boolean },
): LifecycleAction[] {
  if (status === 'candidate') return []
  const out: LifecycleAction[] = []
  if (perms.acknowledge) {
    if (status === 'open') out.push('acknowledge')
    if (status !== 'resolved') out.push('assign')
    if (status === 'open' || status === 'acknowledged') out.push('resolve')
    if (status === 'acknowledged' || status === 'resolved') out.push('reopen')
    out.push('note')
  }
  if (perms.suppress) {
    if (status === 'open' || status === 'acknowledged') out.push('suppress')
    if (status === 'suppressed') out.push('unsuppress', 'resolve')
  }
  return Array.from(new Set(out))
}

export const ACTION_LABEL: Record<LifecycleAction, string> = {
  acknowledge: 'Acknowledge',
  assign: 'Assign',
  suppress: 'Suppress',
  unsuppress: 'Lift suppression',
  resolve: 'Resolve',
  reopen: 'Reopen',
  note: 'Add note',
}

/* ─── Settings form validation (mirrors the server) ────────────────── */

export interface DetectorParam {
  key: string
  label: string
  kind: 'int' | 'float' | 'ports' | 'severity' | 'bool'
  default: unknown
  min: number | null
  max: number | null
  unit: string
  help: string
  value: unknown
}

export interface DetectorSettingsRow extends DetectorStatusRow {
  description: string
  method: string
  params: DetectorParam[]
}

/** Parse a ports field ("22, 3389 445") into sorted unique ports, or an error message. */
export function parsePorts(text: string): { ports: number[] } | { error: string } {
  const parts = text.split(/[\s,]+/).map((p) => p.trim()).filter(Boolean)
  if (!parts.length) return { error: 'Give at least one port' }
  const out = new Set<number>()
  for (const p of parts) {
    if (!/^\d{1,5}$/.test(p)) return { error: `"${p}" is not a port number` }
    const n = Number(p)
    if (n > 65535) return { error: `Port ${n} is outside 0–65535` }
    out.add(n)
  }
  if (out.size > 64) return { error: 'At most 64 ports' }
  return { ports: Array.from(out).sort((a, b) => a - b) }
}

/** Validate a form value for a parameter; returns the typed value or an error message. */
export function validateParam(p: DetectorParam, raw: string | boolean): { value: unknown } | { error: string } {
  if (p.kind === 'bool') return { value: raw === true || raw === 'true' }
  if (p.kind === 'severity') {
    return raw === 'critical' || raw === 'warning' || raw === 'info' ? { value: raw } : { error: 'Choose a severity' }
  }
  if (p.kind === 'ports') {
    const r = parsePorts(String(raw))
    return 'error' in r ? r : { value: r.ports }
  }
  const text = String(raw).trim()
  if (text === '') return { error: 'Required' }
  const n = Number(text)
  if (!Number.isFinite(n)) return { error: 'Must be a number' }
  if (p.kind === 'int' && !Number.isInteger(n)) return { error: 'Must be a whole number' }
  if (p.min !== null && n < p.min) return { error: `Must be at least ${p.min}` }
  if (p.max !== null && n > p.max) return { error: `Must be at most ${p.max}` }
  return { value: n }
}

/** Only what changed, in the `PUT /netflow/detectors` shape. */
export function settingsPatch(
  rows: DetectorSettingsRow[],
  edits: Record<string, Record<string, unknown>>,
): { detectors: Record<string, Record<string, unknown>> } {
  const detectors: Record<string, Record<string, unknown>> = {}
  for (const row of rows) {
    const e = edits[row.kind]
    if (!e) continue
    const changed: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(e)) {
      const current = k === 'enabled' ? row.enabled : row.params.find((p) => p.key === k)?.value
      if (JSON.stringify(current) !== JSON.stringify(v)) changed[k] = v
    }
    if (Object.keys(changed).length) detectors[row.kind] = changed
  }
  return { detectors }
}

/* ─── Threat intel ─────────────────────────────────────────────────── */

export interface TiFeed {
  id: string
  name: string
  source: 'url' | 'upload' | 'builtin'
  url: string | null
  category: string
  enabled: boolean
  refresh_hours: number
  last_refreshed_at: string | null
  last_status: string | null
  entry_count: number
  /** Open + acknowledged threat-intel findings citing this feed (caller's scope). */
  active_findings?: number
}

export interface TiCatalogEntry {
  key: string
  name: string
  url: string
  category: string
  refresh_hours: number
  licence: string
}

export interface TiFeedsResponse {
  items: TiFeed[]
  catalog: TiCatalogEntry[]
  categories: string[]
  index: { prefixes: number; feeds: number }
  can_manage: boolean
  builtin_note: string
}

/** Health of a feed for its status word. */
export function feedState(f: TiFeed): { tone: StatusTone; label: string } {
  if (!f.enabled) return { tone: 'neutral', label: 'Disabled' }
  if (f.last_status?.startsWith('Download failed') || f.last_status?.startsWith('No usable')) return { tone: 'danger', label: 'Failing' }
  if (f.entry_count === 0) return { tone: 'warning', label: 'Empty' }
  return { tone: 'success', label: 'Active' }
}

export interface DetectorException {
  id: string
  kind: string | null
  kind_label: string | null
  src_cidr: string | null
  dst_cidr: string | null
  dst_port: number | null
  reason: string
  expires_at: string | null
  expired: boolean
  created_by: UserRef | null
  created_at: string | null
  matches: number
  /** Exporter scope: every exporter, or the exporters listed (only those the viewer can see). */
  scope: 'global' | 'exporters'
  exporters: string[] | null
  /** The scope also covers exporters outside the viewer's (never named). */
  exporters_outside_scope: boolean
  /** The viewer may change or remove it (netflow.manage and the whole scope inside its own). */
  can_change: boolean
}

/** "10.0.0.5/32" → "10.0.0.5", "2001:db8::1/128" → "2001:db8::1"; networks keep their prefix. */
export function cidrText(c: string | null | undefined): string | null {
  if (!c) return null
  return c.includes(':') ? c.replace(/\/128$/, '') : c.replace(/\/32$/, '')
}

export function exceptionScope(x: Pick<DetectorException, 'src_cidr' | 'dst_cidr' | 'dst_port'>): string {
  const parts: string[] = []
  if (x.src_cidr) parts.push(`from ${cidrText(x.src_cidr)}`)
  if (x.dst_cidr) parts.push(`to ${cidrText(x.dst_cidr)}`)
  if (x.dst_port !== null && x.dst_port !== undefined) parts.push(`port ${x.dst_port}`)
  return parts.join(' ') || 'any traffic'
}

/** Where an exception applies: "Every exporter", or the exporters the viewer can see. */
export function exceptionExporters(x: Pick<DetectorException, 'scope' | 'exporters' | 'exporters_outside_scope'>, max = 3): string {
  if (x.scope === 'global' || !x.exporters) return 'Every exporter'
  const shown = x.exporters.slice(0, max)
  const more = x.exporters.length - shown.length
  let text = x.exporters.length ? `${x.exporters.length === 1 ? 'Exporter' : 'Exporters'} ${shown.join(', ')}` : 'No exporter you can see'
  if (more > 0) text += ` and ${more} more`
  if (x.exporters_outside_scope) text += x.exporters.length ? ', and exporters outside your scope' : ' (exporters outside your scope)'
  return text
}

/** "10.0.0.1, 10.0.0.2 2001:db8::1" → addresses, or the first entry that is not an address. */
export function parseExporterList(raw: string): { exporters: string[] } | { error: string } {
  const parts = raw.split(/[\s,]+/).map((p) => p.trim()).filter(Boolean)
  const out: string[] = []
  for (const p of parts) {
    if (!isIpAddress(p)) return { error: `"${p}" is not an exporter IP address` }
    if (!out.includes(p)) out.push(p)
  }
  return { exporters: out }
}

/** An exception form draft → request body, or field errors. */
export function exceptionBody(d: {
  kind: string
  src: string
  dst: string
  port: string
  reason: string
  expires: string
  /** Optional comma- or space-separated exporter addresses (empty: every exporter in scope). */
  exporters?: string
}): { body: Record<string, unknown> } | { errors: Record<string, string> } {
  const errors: Record<string, string> = {}
  const cidr = /^[0-9a-fA-F:.]+(\/\d{1,3})?$/
  if (d.src.trim() && !cidr.test(d.src.trim())) errors.src = 'An IP address or CIDR'
  if (d.dst.trim() && !cidr.test(d.dst.trim())) errors.dst = 'An IP address or CIDR'
  let port: number | null = null
  if (d.port.trim()) {
    if (!/^\d{1,5}$/.test(d.port.trim()) || Number(d.port) > 65535) errors.port = 'A port from 0 to 65535'
    else port = Number(d.port)
  }
  if (!d.src.trim() && !d.dst.trim() && port === null) errors.src = 'Give a source, destination or port'
  if (!d.reason.trim()) errors.reason = 'Say why this is approved'
  let expires: string | null = null
  if (d.expires.trim()) {
    const ms = Date.parse(d.expires)
    if (!Number.isFinite(ms)) errors.expires = 'Not a date'
    else expires = new Date(ms).toISOString()
  }
  let exporters: string[] = []
  if (d.exporters?.trim()) {
    const parsed = parseExporterList(d.exporters)
    if ('error' in parsed) errors.exporters = parsed.error
    else exporters = parsed.exporters
  }
  if (Object.keys(errors).length) return { errors }
  return {
    body: {
      kind: d.kind || null,
      src_cidr: d.src.trim() || null,
      dst_cidr: d.dst.trim() || null,
      dst_port: port,
      reason: d.reason.trim(),
      expires_at: expires,
      ...(exporters.length ? { exporters } : {}),
    },
  }
}

/* ─── Grouping across collection points (FE-12) ────────────────────── */

/**
 * Findings are per exporter (each has evidence from its own collection point), so traffic seen
 * by several exporters yields one finding per exporter. For reading, they are grouped by detector
 * and subject: the first (strongest) is shown, the others listed under it.
 */
export function subjectKey(f: Pick<Finding, 'kind' | 'src' | 'dst' | 'metrics'>): string {
  const ti = f.metrics?.ti_address
  if (f.kind === 'threat_intel' && typeof ti === 'string') return `${f.kind}|${ti}`
  return `${f.kind}|${f.src ?? ''}|${f.dst ?? ''}`
}

export interface FindingGroup {
  key: string
  primary: Finding
  others: Finding[]
}

export function groupFindings(items: Finding[]): FindingGroup[] {
  const groups = new Map<string, FindingGroup>()
  for (const f of items) {
    const key = subjectKey(f)
    const g = groups.get(key)
    if (g) g.others.push(f)
    else groups.set(key, { key, primary: f, others: [] })
  }
  return Array.from(groups.values())
}
