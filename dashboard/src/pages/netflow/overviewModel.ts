/**
 * Pure logic behind the NetFlow overview (WP B1): response shapes of the overview endpoints,
 * telemetry insights, exporter-health rows, the heatmap scale, the comparison merge and the time
 * axis. Kept free of React so `scripts/test-netflow-overview.mjs` can test it directly.
 */
import { parseIpOrCidr, normalizeIpText } from '@/lib/netflow/ip'
import { formatBps, formatBytes, formatCountExact, formatDuration, formatPercent, ipDisplay } from '@/lib/netflow/format'
import type { NfCoverage, NfExporterState, NfExporterStatusRow } from '@/lib/netflow/types'

/* ─── Response shapes ──────────────────────────────────────────────── */

export type RateStatus = 'ok' | 'beyond_retention' | 'no_receipts'

/**
 * `GET /netflow/overview`. The totals are null (`totals_status: 'beyond_retention'`) when nothing
 * retained can answer the window for this filter — unknown, never zero (X-25).
 */
export interface OverviewTotals {
  bytes: number | null
  packets: number | null
  flows: number | null
  exporters: number | null
  src_hosts: number | null
  dst_hosts: number | null
  /** Distinct addresses on either side. */
  hosts?: number | null
  /** Receipt time of the last matching flow (not exporter liveness). */
  last_seen: string | null
  /** Rate over the last `rate_window_s` seconds of the window; null when unknown (`rate_status`). */
  current_bps: number | null
  rate_window_s?: number
  rate_status?: RateStatus
  totals_status?: 'ok' | 'beyond_retention'
  /** Filter keys the rollups cannot evaluate (they need raw records). */
  raw_only_filters?: string[]
  top_protocol: { protocol: number; name: string; bytes: number } | null
}

export type PointState = 'data' | 'idle' | 'gap' | 'unknown' | 'expired'

/** One bucket of `GET /netflow/timeseries?fill=1` (and of the comparison). */
export interface SeriesPoint {
  i: number
  ts: number
  covered_s: number
  partial: boolean
  open: boolean
  receipt_minutes?: number | null
  bps: number | null
  bytes: number | null
  packets: number | null
  flows: number | null
  state: PointState
}

export interface SeriesCoverage {
  buckets_total: number
  buckets_with_data: number
  buckets_covered?: number
  buckets_gap: number
  buckets_unknown: number
  coverage_pct: number | null
}

export interface CompareWindow {
  from: string
  to: string
  effective_from: string
  effective_to: string
  retention_limited: boolean
  source: string
}

export type DeltaReason = 'prior_beyond_retention' | 'prior_low_coverage' | 'current_low_coverage' | 'no_prior_traffic'

/** `GET /netflow/timeseries/compare`. */
export interface ComparePayload {
  baseline: 'previous_equal' | 'same_time_last_week'
  bucket_s: number
  window: CompareWindow
  prior_window: CompareWindow
  points: SeriesPoint[]
  prior_points: SeriesPoint[]
  totals: {
    bytes: number
    prior_bytes: number
    flows: number
    prior_flows: number
    delta_bytes: number
    delta_pct: number | null
    delta_reason: DeltaReason | null
  }
  coverage: SeriesCoverage
  prior_coverage: SeriesCoverage & { retention_limited: boolean }
  filters: Record<string, string>
  notes: string[]
}

export type RstStatus = 'ok' | 'partial' | 'low_sample' | 'flags_unavailable' | 'no_tcp'
export type DurationStatus = 'ok' | 'partial' | 'unavailable' | 'no_flows'

/** `GET /netflow/device-status` (exporter-health signals). */
export interface DeviceStatusPayload {
  flow_derived: true
  flow_duration_ms: number | null
  flow_duration: { avg_ms: number | null; valid_flows: number; flows: number; coverage_pct: number | null; status: DurationStatus }
  rst_ratio_pct: number | null
  rst: {
    pct: number | null
    status: RstStatus
    rst_flows: number
    tcp_flows: number
    tcp_flows_with_flags: number
    flag_coverage_pct: number | null
    min_sample: number
    ignored_filters: string[]
  }
  flow_continuity_pct: number | null
  continuity: { minutes_with_data: number | null; minutes_tracked: number; coverage_pct: number | null }
  flows: number
  packets: number
  bytes: number
  exporters: number
  last_seen: string | null
}

export type HeatCellState = 'data' | 'partial' | 'zero' | 'no_coverage' | 'unknown' | 'outside_window'

export interface HeatCell {
  dow: number
  hour: number
  bytes: number
  flows: number
  value: number | null
  occurrences: number
  covered_occurrences: number
  dates_with_data: number
  state: HeatCellState
}

/** `GET /netflow/heatmap`. */
export interface HeatmapPayload {
  max_bytes: number
  cells: HeatCell[]
  from: string
  to: string
  effective_from: string
  effective_to: string
  tz: string
  basis: 'avg' | 'sum'
  value_unit: 'bytes_per_hour' | 'bytes'
  max_value: number
  coverage: { tracking_from: string | null; hours_in_window: number; hours_covered: number; coverage_pct: number | null }
}

/** One row of `GET /netflow/exporters`. */
export interface ExporterTraffic {
  exporter_ip: string
  bytes: number
  packets: number
  flows: number
  last_seen: string | null
  last_flow_in_scope?: string | null
  last_received_at?: string | null
}

export interface ExplainItem {
  key: string
  label: string
  filter: Record<string, string | number>
  current_bytes: number
  baseline_bytes: number
  delta_bytes: number
  delta_pct: number | null
  current_flows: number
  baseline_flows: number
  share_of_change_pct: number | null
  share_of_current_pct: number | null
  trend: 'up' | 'down' | 'new' | 'gone' | 'flat'
}

/** `GET|POST /netflow/explain`. */
export interface ExplainPayload {
  baseline: 'previous_equal' | 'same_time_last_week'
  window: { from: string; to: string; effective_from: string; effective_to: string; source: string }
  baseline_window: { from: string; to: string; effective_from: string; effective_to: string; source: string; retention_limited: boolean }
  totals: {
    current_bytes: number
    baseline_bytes: number
    current_flows: number
    baseline_flows: number
    delta_bytes: number
    delta_pct: number | null
    delta_reason: DeltaReason | null
  }
  coverage: {
    current: { minutes_with_data: number | null; minutes_tracked: number; coverage_pct: number | null }
    baseline: { minutes_with_data: number | null; minutes_tracked: number; coverage_pct: number | null }
  }
  dimensions: { dimension: string; label: string; items: ExplainItem[]; explained_pct: number | null; other_delta_bytes: number }[]
  filters: Record<string, string>
  method: string
}

/** The part of `GET /netflow/findings` (contract §11) the overview reads. */
export interface FindingsSummary {
  /** by_severity counts active (open + acknowledged) findings; by_severity_open only open ones. */
  counts?: { open?: number; acknowledged?: number; suppressed?: number; resolved?: number; by_severity?: Record<string, number>; by_severity_open?: Record<string, number> }
  detectors?: { kind: string; status: string; reason?: string | null }[]
}

/* ─── Text helpers ─────────────────────────────────────────────────── */

export const DELTA_REASON_TEXT: Record<DeltaReason, string> = {
  prior_beyond_retention: 'the comparison period is partly older than the data kept',
  prior_low_coverage: 'records arrived in less than half of the comparison period',
  current_low_coverage: 'records arrived in less than half of this period',
  no_prior_traffic: 'the comparison period had no matching traffic',
}

export function deltaReasonText(reason: DeltaReason | null | undefined): string {
  return reason ? DELTA_REASON_TEXT[reason] ?? 'the comparison is not reliable' : ''
}

/** "+12.5%" / "−3.1%" (a real minus sign), "—" when unknown. */
export function formatDelta(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  const abs = formatPercent(Math.abs(pct))
  return pct > 0 ? `+${abs}` : pct < 0 ? `−${abs}` : '0%'
}

/**
 * The rate KPI names what it is: for a window ending now it is the recent rate, for a historical
 * window it is the rate at the end of the selected window, never "current".
 */
export function rateLabel(windowEndsNow: boolean, seconds: number | null | undefined): { label: string; hint: string } {
  const span = seconds ? formatDuration(seconds * 1000) : '5 min'
  return windowEndsNow
    ? { label: `Rate, last ${span}`, hint: `Average rate over the last ${span}; flows are reported when they end, so the newest minutes can read low` }
    : { label: 'Rate at end of window', hint: `Average rate over the last ${span} of the selected window` }
}

/**
 * Why the rate is unknown (null), or null when it is a value. A window whose exporters delivered
 * nothing over the rate minutes is "unknown", never a confident 0 bps (contract §1).
 */
export function rateUnknownText(status: RateStatus | null | undefined, windowEndsNow: boolean, seconds: number | null | undefined): string | null {
  const span = seconds ? formatDuration(seconds * 1000) : '5 min'
  if (status === 'no_receipts') {
    return windowEndsNow ? `No records received in the last ${span}` : `No records received in the last ${span} of the window`
  }
  if (status === 'beyond_retention') return 'Raw records for this moment have expired'
  return null
}

export const RST_STATUS_TEXT: Record<RstStatus, string> = {
  ok: 'over TCP records with exported flags',
  partial: 'flags exported for only part of the TCP records',
  low_sample: 'too few TCP records with flags for a ratio',
  flags_unavailable: 'the exporters do not send TCP flags',
  no_tcp: 'no TCP traffic in scope',
}

export const DURATION_STATUS_TEXT: Record<DurationStatus, string> = {
  ok: 'mean of flows with valid start/end times',
  partial: 'only part of the flows carry valid start/end times',
  unavailable: 'the exporters do not send usable flow start/end times',
  no_flows: 'no flows in scope',
}

/** "+10.5 GB" / "−123 MB" (a real minus sign). */
export function signedBytes(n: number): string {
  return `${n >= 0 ? '+' : '−'}${formatBytes(Math.abs(n))}`
}

/** An item's part in the total change: a share when it moved the same way, an offset when against. */
export function shareOfChangeText(sharePct: number | null): string | null {
  if (sharePct == null || !Number.isFinite(sharePct)) return null
  return sharePct >= 0 ? `${formatPercent(sharePct)} of the change` : `offsets ${formatPercent(Math.abs(sharePct))} of the change`
}

/* ─── Host filter (NF-26) ──────────────────────────────────────────── */

/** Validate the header's host box: an IPv4/IPv6 address or CIDR, applied as `talker`. */
export function parseHostFilter(raw: string): { ok: true; value: string | null } | { ok: false; error: string } {
  const v = raw.trim()
  if (!v) return { ok: true, value: null }
  const parsed = parseIpOrCidr(v)
  if (!parsed) return { ok: false, error: 'Enter an IP address or CIDR, e.g. 10.0.0.5 or 10.0.0.0/24' }
  return { ok: true, value: normalizeIpText(v) }
}

/* ─── Exporter state ───────────────────────────────────────────────── */

export const STALE_S = 120
export const SILENT_S = 900

/** Receipt state from an unfiltered receipt time on the appliance clock (fallback only). */
export function receiptState(lastReceivedAt: string | null | undefined, nowMs: number, staleS = STALE_S, silentS = SILENT_S): { state: NfExporterState; ageS: number | null } {
  if (!lastReceivedAt) return { state: 'never', ageS: null }
  const t = Date.parse(lastReceivedAt)
  if (!Number.isFinite(t)) return { state: 'never', ageS: null }
  const ageS = Math.max(0, Math.round((nowMs - t) / 1000))
  return { state: ageS < staleS ? 'receiving' : ageS < silentS ? 'late' : 'silent', ageS }
}

export type HealthMode = 'live' | 'estimated' | 'historical'

export interface ExporterHealthRow {
  exporter: string
  name: string
  deviceHostname: string | null
  /** Current device reachability; only for a window ending now (it does not describe the past). */
  deviceStatus: string | null
  /** Whether the exporter is mapped to a device: null when the status service did not say. */
  mapped: boolean | null
  mode: HealthMode
  state: NfExporterState | null
  ageS: number | null
  clockOffsetMs: number | null
  clockSuspect: boolean
  /** Historical windows: share of the window's minutes with delivered records. */
  coveragePct: number | null
  bytes: number | null
  flows: number | null
}

export interface ExporterHealthInput {
  windowEndsNow: boolean
  status: { available: boolean; rows: NfExporterStatusRow[] } | null
  traffic: ExporterTraffic[]
  coverage: NfCoverage | null
  nowMs: number
  /** Deep-dive page: only this exporter. */
  exporter?: string
}

const STATE_ORDER: Record<NfExporterState, number> = { silent: 0, late: 1, never: 2, receiving: 3 }
const MODE_ORDER: Record<HealthMode, number> = { live: 0, estimated: 1, historical: 2 }

/**
 * One row per exporter for the health card. A window ending now shows the appliance's receipt
 * state (`/exporters/status`, or — when that is unavailable — an estimate from the unfiltered
 * receipt time on the calibrated clock). A historical window shows how much of that window each
 * exporter delivered, never today's state as if it described the past (NF-01); the status
 * service still names the exporter and its device there, but its live state is not used.
 */
export function exporterHealthRows(input: ExporterHealthInput): ExporterHealthRow[] {
  const traffic = new Map(input.traffic.map((t) => [ipDisplay(t.exporter_ip), t]))
  const coverage = new Map((input.coverage?.exporters ?? []).map((c) => [ipDisplay(c.exporter), c]))
  const identity = new Map((input.status?.available ? input.status.rows : []).map((s) => [ipDisplay(s.exporter), s]))
  const rows: ExporterHealthRow[] = []
  const seen = new Set<string>()
  const add = (ip: string, r: Partial<ExporterHealthRow>) => {
    if (input.exporter && ipDisplay(input.exporter) !== ip) return
    if (seen.has(ip)) return
    seen.add(ip)
    const t = traffic.get(ip)
    const c = coverage.get(ip)
    const id = identity.get(ip)
    const cov = c && c.minutes_in_window ? Math.min(100, (100 * c.minutes_with_data) / c.minutes_in_window) : null
    rows.push({
      exporter: ip,
      name: r.name ?? (id?.display_name || ip),
      deviceHostname: r.deviceHostname ?? id?.device_hostname ?? null,
      deviceStatus: r.deviceStatus ?? null,
      mapped: id ? !!id.device_hostname : null,
      mode: r.mode ?? 'historical',
      state: r.state ?? null,
      ageS: r.ageS ?? null,
      clockOffsetMs: r.clockOffsetMs ?? null,
      clockSuspect: r.clockSuspect ?? false,
      coveragePct: cov,
      bytes: t ? t.bytes : null,
      flows: t ? t.flows : null,
    })
  }
  if (input.windowEndsNow && input.status?.available) {
    for (const s of input.status.rows) {
      const ip = ipDisplay(s.exporter)
      add(ip, {
        name: s.display_name || ip,
        deviceHostname: s.device_hostname,
        deviceStatus: s.device_status,
        mode: 'live',
        state: s.state,
        ageS: s.receipt_age_s,
        clockOffsetMs: s.clock_offset_ms,
        clockSuspect: !!s.clock_suspect,
      })
    }
  }
  for (const t of input.traffic) {
    const ip = ipDisplay(t.exporter_ip)
    if (input.windowEndsNow) {
      const est = receiptState(t.last_received_at ?? null, input.nowMs)
      add(ip, { mode: 'estimated', state: est.state, ageS: est.ageS })
    } else {
      add(ip, { mode: 'historical' })
    }
  }
  if (!input.windowEndsNow) {
    for (const c of input.coverage?.exporters ?? []) add(ipDisplay(c.exporter), { mode: 'historical' })
  }
  return rows.sort((a, b) => {
    const sa = a.state ? STATE_ORDER[a.state] : 4
    const sb = b.state ? STATE_ORDER[b.state] : 4
    if (sa !== sb) return sa - sb
    // Within a state, the appliance's own verdict before an estimate.
    if (a.mode !== b.mode) return MODE_ORDER[a.mode] - MODE_ORDER[b.mode]
    return (b.bytes ?? -1) - (a.bytes ?? -1) || a.exporter.localeCompare(b.exporter)
  })
}

/* ─── Telemetry insights (NF-23) ───────────────────────────────────── */

export type InsightSeverity = 'critical' | 'warning' | 'info'

export interface Insight {
  id: string
  severity: InsightSeverity
  title: string
  body: string
  /** Internal link for the next step (Anomalies, Collector health, exporter page). */
  link?: { to: string; label: string }
  /**
   * Items of one kind (e.g. every silent exporter). The card folds three or more of them into one
   * box with a compact line each; every item still counts as actionable.
   */
  group?: { key: string; title: string }
  /** The compact line used inside a group ("127.0.0.81 · 23 min without flows"). */
  short?: string
}

/** One box in the insights list: a single item, or a folded group of same-kind items. */
export type InsightBlock =
  | { kind: 'item'; insight: Insight }
  | { kind: 'group'; key: string; title: string; severity: InsightSeverity; items: Insight[] }

/** Fold runs of same-group items (>= `min`) into one block, at the position of the first. */
export function groupInsights(items: Insight[], min = 3): InsightBlock[] {
  const counts = new Map<string, number>()
  for (const i of items) if (i.group) counts.set(i.group.key, (counts.get(i.group.key) ?? 0) + 1)
  const out: InsightBlock[] = []
  const groups = new Map<string, Extract<InsightBlock, { kind: 'group' }>>()
  for (const i of items) {
    const key = i.group?.key
    if (!key || (counts.get(key) ?? 0) < min) {
      out.push({ kind: 'item', insight: i })
      continue
    }
    let g = groups.get(key)
    if (!g) {
      g = { kind: 'group', key, title: `${counts.get(key)} ${i.group!.title}`, severity: i.severity, items: [] }
      groups.set(key, g)
      out.push(g)
    }
    g.items.push(i)
    if (SEVERITY_RANK[i.severity] < SEVERITY_RANK[g.severity]) g.severity = i.severity
  }
  return out
}

export interface InsightsInput {
  windowEndsNow: boolean
  health: ExporterHealthRow[]
  statusAvailable: boolean | null
  device: DeviceStatusPayload | null
  findings: { available: boolean; data: FindingsSummary | null } | null
  coverage: NfCoverage | null
  totals: OverviewTotals | null
  links: { anomalies: string; health: string; exporter: (ip: string) => string }
  /**
   * Inputs whose query failed with nothing to show (NF-03): their checks did not run, so the
   * actionable count is incomplete and must not read as "nothing actionable". `exporterState`:
   * neither the receipt status nor the exporter list answered; `signals`: the flow-derived TCP
   * reset/flag signals; `coverage`: delivery coverage. Findings say it through `findings.available`.
   */
  failed?: { exporterState?: boolean; signals?: boolean; coverage?: boolean }
}

export interface InsightsResult {
  actionable: Insight[]
  context: Insight[]
  maxSeverity: InsightSeverity | null
  /** What could not be evaluated because its query failed (empty or absent when everything answered). */
  unavailable?: string[]
  /** Every input failed: nothing was evaluated, so there is no count at all. */
  allUnavailable?: boolean
}

/** Labels of the insight inputs, as listed when they could not be evaluated. */
export const INSIGHT_INPUT_LABEL = {
  exporterState: 'exporter receipt state',
  signals: 'TCP reset and flag signals',
  findings: 'anomaly findings',
  coverage: 'delivery coverage',
} as const

/**
 * Whether the exporter list itself is unknown (NF-03): every source of it failed, or one failed
 * and the others listed nothing (a failed source may hold the exporters). A window ending now
 * lists exporters from the receipt status and the window's traffic; a past window from its
 * traffic and delivery coverage. Then the card shows no count and never "no exporter has sent
 * flows".
 */
export function exporterListUnknown(i: {
  windowEndsNow: boolean
  rows: number
  statusFailed: boolean
  trafficFailed: boolean
  coverageFailed: boolean
}): boolean {
  const sources = i.windowEndsNow ? [i.statusFailed, i.trafficFailed] : [i.trafficFailed, i.coverageFailed]
  if (sources.every(Boolean)) return true
  return i.rows === 0 && sources.some(Boolean)
}

const SEVERITY_RANK: Record<InsightSeverity, number> = { critical: 0, warning: 1, info: 2 }

function describeDevice(row: ExporterHealthRow): string {
  if (!row.deviceHostname) {
    return row.mapped === false
      ? 'The exporter is not mapped to a device, so its reachability is unknown.'
      : 'Its device reachability is unknown.'
  }
  const status = (row.deviceStatus || '').toLowerCase()
  if (!status || status === 'unknown') return `Device ${row.deviceHostname}: reachability unknown.`
  if (status === 'up' || status === 'online' || status === 'ok') {
    return `Device ${row.deviceHostname} is up, so check its flow-export configuration or the path to the collector.`
  }
  return `Device ${row.deviceHostname} reports status "${row.deviceStatus}".`
}

/**
 * Telemetry insights for the window: `actionable` items (counted, ordered by severity, never
 * capped) and `context` lines (normal activity, never counted). Exporter silence is reported as
 * silence — with the device's own reachability when it is known — and only for a window that
 * ends now; historical windows never claim a current outage. No hard-coded bandwidth thresholds:
 * capacity risks belong to Capacity, which knows each link's speed.
 */
export function buildTelemetryInsights(input: InsightsInput): InsightsResult {
  const actionable: Insight[] = []
  const context: Insight[] = []

  if (input.windowEndsNow) {
    for (const row of input.health) {
      const age = row.ageS != null ? formatDuration(row.ageS * 1000) : null
      const estimated = row.mode === 'estimated' ? ' (estimated from the last receipt time)' : ''
      if (row.state === 'silent') {
        actionable.push({
          id: `silent-${row.exporter}`,
          severity: 'critical',
          title: `Exporter silent: no flows from ${row.name} for ${age ?? 'a long time'}`,
          body: `${describeDevice(row)}${estimated}`,
          link: { to: input.links.exporter(row.exporter), label: 'Open exporter' },
          group: { key: 'exporter-silent', title: 'exporters silent' },
          short: `${row.name} · no flows for ${age ?? 'a long time'}`,
        })
      } else if (row.state === 'late') {
        actionable.push({
          id: `late-${row.exporter}`,
          severity: 'warning',
          title: `Exporter late: last flows from ${row.name} ${age ? `${age} ago` : 'a while ago'}`,
          body: `Longer than the expected export interval${estimated}. ${describeDevice(row)}`,
          link: { to: input.links.exporter(row.exporter), label: 'Open exporter' },
          group: { key: 'exporter-late', title: 'exporters late' },
          short: `${row.name} · last flows ${age ? `${age} ago` : 'a while ago'}`,
        })
      } else if (row.state === 'never') {
        actionable.push({
          id: `never-${row.exporter}`,
          severity: 'warning',
          title: `No flows ever received from ${row.name}`,
          body: 'The exporter is configured on this appliance but the collector has no record of its flows.',
          link: { to: input.links.health, label: 'Collector health' },
          group: { key: 'exporter-never', title: 'exporters never delivered flows' },
          short: row.name,
        })
      }
      if (row.clockSuspect) {
        const off = row.clockOffsetMs != null ? formatDuration(Math.abs(row.clockOffsetMs)) : null
        const dir = row.clockOffsetMs != null ? (row.clockOffsetMs > 0 ? 'behind' : 'ahead of') : 'out of step with'
        actionable.push({
          id: `clock-${row.exporter}`,
          severity: 'warning',
          title: `Exporter clock ${off ? `${off} ${dir}` : dir} the appliance: ${row.name}`,
          body: 'Flow times from this exporter are shifted; the collector keeps receipt times on the appliance clock.',
          link: { to: input.links.health, label: 'Collector health' },
          group: { key: 'exporter-clock', title: 'exporter clocks out of step with the appliance' },
          short: `${row.name} · ${off ? `${off} ${dir}` : dir} the appliance`,
        })
      }
    }
    if (input.failed?.exporterState) {
      context.push({
        id: 'status-unavailable',
        severity: 'info',
        title: 'Exporter receipt state unavailable',
        body: 'Neither the collector status nor the exporter list answered, so silent or late exporters cannot be listed.',
      })
    } else if (input.statusAvailable === false) {
      context.push({
        id: 'status-unavailable',
        severity: 'info',
        title: 'Exporter receipt state unavailable',
        body: 'The collector status endpoint did not answer; states shown are estimated from the last receipt time.',
      })
    }
  } else {
    context.push({
      id: 'historical',
      severity: 'info',
      title: 'Historical window',
      body: 'Live exporter state is not evaluated for a past window; the health card shows what each exporter delivered then.',
    })
  }

  const rst = input.device?.rst
  if (rst) {
    if ((rst.status === 'ok' || rst.status === 'partial') && rst.pct != null && rst.pct >= 5) {
      actionable.push({
        id: 'rst-high',
        severity: 'warning',
        title: `High TCP reset share: ${formatPercent(rst.pct)}`,
        body: `${formatCountExact(rst.rst_flows)} of ${formatCountExact(rst.tcp_flows_with_flags)} TCP records with exported flags carried RST (aborted connections or probes).`,
        link: { to: input.links.anomalies, label: 'Anomalies' },
      })
    } else if (rst.status === 'flags_unavailable') {
      actionable.push({
        id: 'flags-missing',
        severity: 'info',
        title: 'TCP flags are not exported: SYN/RST checks unavailable',
        body: 'Add TCP flags to the exporter template; until then scan and reset detection cannot see TCP handshakes.',
        link: { to: input.links.health, label: 'Collector health' },
      })
    }
  }

  const findings = input.findings
  if (findings) {
    if (!findings.available) {
      context.push({ id: 'findings-unavailable', severity: 'info', title: 'Anomaly findings unavailable', body: 'The findings service did not answer.' })
    } else {
      const open = findings.data?.counts?.open ?? null
      // "N open findings, X of them high or critical": both counts over open findings only.
      const bySev = findings.data?.counts?.by_severity_open ?? {}
      const critical = (bySev.critical ?? 0) + (bySev.high ?? 0)
      if (open != null && open > 0) {
        actionable.push({
          id: 'findings',
          severity: critical > 0 ? 'critical' : 'warning',
          title: `${formatCountExact(open)} open anomaly finding${open === 1 ? '' : 's'}`,
          body: critical > 0 ? `${formatCountExact(critical)} of them high or critical severity.` : 'Review, acknowledge or suppress them in Anomalies.',
          link: { to: input.links.anomalies, label: 'Anomalies' },
        })
      } else if (open === 0) {
        context.push({ id: 'findings-none', severity: 'info', title: 'No open anomaly findings', body: 'Detectors report no open findings; unavailable detectors are listed in Anomalies.', link: { to: input.links.anomalies, label: 'Anomalies' } })
      }
      const down = (findings.data?.detectors ?? []).filter((d) => d.status !== 'ok' && d.status !== 'disabled')
      if (down.length) {
        context.push({
          id: 'detectors-limited',
          severity: 'info',
          title: `${down.length} detector${down.length === 1 ? '' : 's'} cannot run`,
          body: down.map((d) => `${d.kind}: ${d.reason || d.status}`).join('; '),
          link: { to: input.links.anomalies, label: 'Anomalies' },
        })
      }
    }
  }

  const t = input.coverage?.totals
  if (t && typeof t.minutes_with_data === 'number' && typeof t.minutes_in_window === 'number' && t.minutes_in_window > 0) {
    const pct = (100 * t.minutes_with_data) / t.minutes_in_window
    if (pct < 90) {
      actionable.push({
        id: 'coverage',
        severity: pct < 50 ? 'warning' : 'info',
        title: `Records received in ${formatPercent(pct)} of this window's minutes`,
        body: 'Minutes without records are collection gaps, not idle traffic; totals for this window are incomplete.',
        link: { to: input.links.health, label: 'Collector health' },
      })
    }
  }

  const totals = input.totals
  if (totals) {
    if (totals.current_bps != null) {
      context.push({ id: 'rate', severity: 'info', title: `Rate ${formatBps(totals.current_bps)}`, body: input.windowEndsNow ? 'Over the last minutes of the window.' : 'At the end of the selected window.' })
    }
    if (totals.bytes != null) context.push({
      id: 'volume',
      severity: 'info',
      title: `${formatBytes(totals.bytes)} in ${formatCountExact(totals.flows)} flow records`,
      body: totals.top_protocol ? `Largest protocol: ${totals.top_protocol.name} (${formatBytes(totals.top_protocol.bytes)}).` : 'No protocol breakdown.',
    })
  }

  // Inputs that did not answer: their checks did not run (NF-03). With every input failed there
  // is nothing to count; with some, the count covers only what answered.
  const unavailable: string[] = []
  const evaluated = input.windowEndsNow ? 4 : 3
  if (input.windowEndsNow && input.failed?.exporterState) unavailable.push(INSIGHT_INPUT_LABEL.exporterState)
  if (input.failed?.signals) unavailable.push(INSIGHT_INPUT_LABEL.signals)
  if (findings && !findings.available) unavailable.push(INSIGHT_INPUT_LABEL.findings)
  if (input.failed?.coverage) unavailable.push(INSIGHT_INPUT_LABEL.coverage)

  actionable.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity])
  return {
    actionable,
    context,
    maxSeverity: actionable.length ? actionable[0].severity : null,
    unavailable,
    allUnavailable: unavailable.length >= evaluated,
  }
}

/** "a", "a and b", "a, b and c". */
export function joinList(items: string[]): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/**
 * The insights badge and empty-list sentence (NF-03): a count only when every input answered;
 * "Unavailable" when none did; otherwise the count of what answered, marked incomplete. A
 * pending evaluation shows no count either.
 */
export function insightsSummary(r: InsightsResult, loading: boolean): { badge: string; aria: string; empty: string | null; incomplete: boolean } {
  const n = r.actionable.length
  const unavailable = r.unavailable ?? []
  const items = `actionable item${n === 1 ? '' : 's'}`
  if (r.allUnavailable) {
    return {
      badge: 'Unavailable',
      aria: 'Insights unavailable: their queries failed',
      empty: `Insights could not be evaluated: the ${joinList(unavailable)} queries failed. This is not a clean bill of health.`,
      incomplete: true,
    }
  }
  if (loading && n === 0) return { badge: '…', aria: 'Evaluating the window', empty: null, incomplete: false }
  if (unavailable.length) {
    return {
      badge: n ? `${n}+ actionable` : 'Incomplete',
      aria: n ? `At least ${n} ${items}; not evaluated: ${joinList(unavailable)}` : `Incomplete: not evaluated: ${joinList(unavailable)}`,
      empty: n
        ? null
        : `Nothing actionable in what answered, but ${joinList(unavailable)} could not be evaluated (query failed), so this check is incomplete.`,
      incomplete: true,
    }
  }
  return {
    badge: `${n} actionable`,
    aria: `${n} ${items}`,
    empty: n ? null : "Nothing actionable in this window's flow telemetry. This is not a security clearance: see Anomalies for detector coverage.",
    incomplete: false,
  }
}

/* ─── Heatmap scale (NF-28) ────────────────────────────────────────── */

export const HEAT_STEPS = 6

/** Sequential single-hue blue, light → dark (light theme) and dark → light (dark theme). */
export const HEAT_COLORS = {
  light: ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95'],
  dark: ['#104281', '#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#cde2fb'],
} as const

/** Bin index 0..HEAT_STEPS-1 of a positive value on a linear scale; -1 for zero or unknown. */
export function heatBin(value: number | null | undefined, max: number): number {
  if (value == null || !Number.isFinite(value) || value <= 0 || !(max > 0)) return -1
  return Math.min(HEAT_STEPS - 1, Math.floor((value / max) * HEAT_STEPS))
}

/** Numeric legend: the lower bound of every bin (linear scale from 0 to `max`). */
export function heatLegend(max: number): { lo: number; hi: number }[] {
  if (!(max > 0)) return []
  return Array.from({ length: HEAT_STEPS }, (_, i) => ({ lo: (max * i) / HEAT_STEPS, hi: (max * (i + 1)) / HEAT_STEPS }))
}

export const HOUR_TICKS = [0, 6, 12, 18, 23] as const

export const HEAT_STATE_TEXT: Record<HeatCellState, string> = {
  data: 'traffic in every occurrence',
  partial: 'traffic, with collection gaps in some occurrences',
  zero: 'records received, none matched',
  no_coverage: 'no records received (collection gap)',
  unknown: 'before delivery tracking began',
  outside_window: 'not in the selected window',
}

/* ─── Throughput chart data (NF-02, X-35) ──────────────────────────── */

export interface ChartRow {
  /** Bucket start (ms) — the x position. */
  x: number
  /** Bucket end (ms), clamped to the window. */
  xEnd: number
  bps: number | null
  bytes: number | null
  /** `prior_only`: a previous-period bucket with no bucket of this window at its index. */
  state: PointState | 'prior_only'
  partial: boolean
  open: boolean
  prior_bps?: number | null
  prior_bytes?: number | null
  prior_ts?: number | null
  prior_state?: PointState | null
}

const hasRate = (p: SeriesPoint | undefined): p is SeriesPoint => !!p && (p.state === 'data' || p.state === 'idle')

/**
 * Chart rows positioned on a real time scale. Gaps stay null so the area breaks; with a comparison
 * the prior series is paired by bucket index `i` (never by `ts + span`, which drew nothing for
 * spans that were not a bucket multiple), and each row keeps the prior bucket's own time.
 *
 * The two periods' bucket grids can differ by one bucket (a span that is not a bucket multiple
 * starts and ends at different offsets in each): a prior bucket with no current bucket at its
 * index gets a row of its own at that index's position (clamped to the window end), so every
 * prior bucket counted in the prior total is plotted and listed (NF-02).
 */
export function chartRows(points: SeriesPoint[], bucketS: number, windowEndMs: number, prior?: SeriesPoint[] | null): ChartRow[] {
  const byIndex = new Map((prior ?? []).map((p) => [p.i, p]))
  const rows: ChartRow[] = points.map((p) => {
    const row: ChartRow = {
      x: p.ts,
      xEnd: Math.min(p.ts + bucketS * 1000, windowEndMs),
      bps: hasRate(p) ? p.bps : null,
      bytes: p.bytes,
      state: p.state,
      partial: p.partial,
      open: p.open,
    }
    if (prior) {
      const q = byIndex.get(p.i)
      row.prior_bps = hasRate(q) ? q.bps : null
      row.prior_bytes = q ? q.bytes : null
      row.prior_ts = q ? q.ts : null
      row.prior_state = q ? q.state : null
    }
    return row
  })
  if (!prior?.length || !points.length) return rows
  const current = new Set(points.map((p) => p.i))
  const bucketMs = bucketS * 1000
  const origin = points[0].ts - points[0].i * bucketMs
  const extra = prior
    .filter((q) => !current.has(q.i))
    .map((q): ChartRow => {
      const x = Math.min(origin + q.i * bucketMs, windowEndMs)
      return {
        x,
        xEnd: Math.min(x + bucketMs, windowEndMs),
        bps: null,
        bytes: null,
        state: 'prior_only',
        partial: false,
        open: false,
        prior_bps: hasRate(q) ? q.bps : null,
        prior_bytes: q.bytes,
        prior_ts: q.ts,
        prior_state: q.state,
      }
    })
  return extra.length ? [...rows, ...extra].sort((a, b) => a.x - b.x) : rows
}

/** Rows whose `key` value stands alone (null on both sides): drawn as a dot, since a line needs two points. */
export function isolatedRows(rows: ChartRow[], key: 'bps' | 'prior_bps'): boolean[] {
  return rows.map((r, i) => r[key] != null && rows[i - 1]?.[key] == null && rows[i + 1]?.[key] == null)
}

/** Consecutive runs of buckets in `states` as [from, to) spans (ms) for shading. */
export function stateSpans(rows: ChartRow[], states: PointState[]): { from: number; to: number; state: PointState }[] {
  const out: { from: number; to: number; state: PointState }[] = []
  for (const r of rows) {
    if (r.state === 'prior_only' || !states.includes(r.state)) continue
    const last = out[out.length - 1]
    if (last && last.to === r.x && last.state === r.state) last.to = r.xEnd
    else out.push({ from: r.x, to: r.xEnd, state: r.state })
  }
  return out
}

export interface SeriesSummary {
  peakBps: number | null
  peakAt: number | null
  avgBps: number | null
  gaps: number
  idle: number
  buckets: number
  /**
   * Buckets left out of the peak and the axis scale: the one still filling, and edge buckets
   * covering less than half a bucket (their rate divides by a few seconds and overstates bursts).
   */
  peakExcluded: number
  /** The peak had to come from such buckets (no complete bucket had data). */
  peakFromShort: boolean
}

/** Whether a bucket's rate is reliable enough for the peak and the axis scale. */
export function isFullBucket(p: Pick<SeriesPoint, 'open' | 'partial' | 'covered_s'>, bucketS?: number | null): boolean {
  if (p.open) return false
  if (!p.partial) return true
  return bucketS ? p.covered_s >= bucketS / 2 : false
}

/**
 * Peak and average over buckets with data; the average weights each bucket by its covered seconds.
 * The peak skips the bucket still filling and edge buckets under half a bucket, whose rate over a
 * few seconds would show a false spike; `peakExcluded` says how many were skipped.
 */
export function summarizeSeries(points: SeriesPoint[], bucketS?: number | null): SeriesSummary {
  let peak: number | null = null
  let peakAt: number | null = null
  let shortPeak: number | null = null
  let shortPeakAt: number | null = null
  let bits = 0
  let seconds = 0
  let gaps = 0
  let idle = 0
  let excluded = 0
  for (const p of points) {
    if (p.state === 'gap') gaps++
    if (p.state === 'idle') idle++
    if ((p.state === 'data' || p.state === 'idle') && p.bps != null && p.covered_s > 0) {
      bits += p.bps * p.covered_s
      seconds += p.covered_s
      if (isFullBucket(p, bucketS)) {
        if (peak == null || p.bps > peak) {
          peak = p.bps
          peakAt = p.ts
        }
      } else {
        excluded++
        if (shortPeak == null || p.bps > shortPeak) {
          shortPeak = p.bps
          shortPeakAt = p.ts
        }
      }
    }
  }
  const fromShort = peak == null && shortPeak != null
  return {
    peakBps: fromShort ? shortPeak : peak,
    peakAt: fromShort ? shortPeakAt : peakAt,
    avgBps: seconds ? bits / seconds : null,
    gaps,
    idle,
    buckets: points.length,
    peakExcluded: fromShort ? 0 : excluded,
    peakFromShort: fromShort,
  }
}

/** The highest reliable rate across series (the y-axis scale), null when there is none. */
export function maxForScale(series: (SeriesPoint[] | null | undefined)[], bucketS?: number | null): number | null {
  let max: number | null = null
  for (const pts of series) {
    for (const p of pts ?? []) {
      if ((p.state === 'data' || p.state === 'idle') && p.bps != null && isFullBucket(p, bucketS)) {
        max = max == null ? p.bps : Math.max(max, p.bps)
      }
    }
  }
  return max
}

/* ─── Time axis in the viewer's zone ───────────────────────────────── */

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR
const TICK_STEPS = [MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY]

/** UTC offset (ms) of `tz` at instant `ms`. */
export function tzOffsetMs(ms: number, tz: string): number {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(ms))
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0)
    const asUtc = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'))
    return asUtc - Math.floor(ms / 1000) * 1000
  } catch {
    return 0
  }
}

/** Evenly stepped ticks aligned to local (tz) clock boundaries, at most `max` of them. */
export function timeTicks(fromMs: number, toMs: number, tz: string, max = 7): number[] {
  const span = Math.max(1, toMs - fromMs)
  const step = TICK_STEPS.find((s) => span / s <= max) ?? TICK_STEPS[TICK_STEPS.length - 1]
  const ticks: number[] = []
  const off = tzOffsetMs(fromMs, tz)
  let t = Math.ceil((fromMs + off) / step) * step - off
  while (t <= toMs && ticks.length < 100) {
    // Re-align across DST changes: keep the tick on a local boundary.
    const o = tzOffsetMs(t, tz)
    const aligned = Math.round((t + o) / step) * step - o
    if (aligned >= fromMs && aligned <= toMs && !ticks.includes(aligned)) ticks.push(aligned)
    t += step
  }
  return ticks
}

/** Tick label for a span: times within a day, weekday + time within a week, dates beyond. */
export function formatTick(ms: number, spanMs: number, tz: string): string {
  const opts: Intl.DateTimeFormatOptions = { timeZone: tz, hourCycle: 'h23' }
  if (spanMs <= 36 * HOUR) Object.assign(opts, { hour: '2-digit', minute: '2-digit' })
  else if (spanMs <= 8 * DAY) Object.assign(opts, { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  else Object.assign(opts, { month: 'short', day: 'numeric' })
  try {
    return new Intl.DateTimeFormat('en-GB', opts).format(new Date(ms))
  } catch {
    return new Date(ms).toISOString().slice(11, 16)
  }
}

/* ─── Hour/day filters across zones (NF-28) ────────────────────────── */

const WEEKDAY_INDEX: Record<string, number> = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }
const QUARTER_HOUR = 15 * MIN

/** ISO weekday (1 = Monday), hour and minute of instant `ms` in `tz`. */
export function zonedParts(ms: number, tz: string): { dow: number; hour: number; minute: number } {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(ms))
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
    return { dow: WEEKDAY_INDEX[get('weekday')] ?? 1, hour: Number(get('hour')) % 24, minute: Number(get('minute')) }
  } catch {
    const d = new Date(ms)
    return { dow: ((d.getUTCDay() + 6) % 7) + 1, hour: d.getUTCHours(), minute: d.getUTCMinutes() }
  }
}

const matchesHourDow = (p: { dow: number; hour: number }, hour?: number | null, dow?: number | null) =>
  (hour == null || p.hour === hour) && (dow == null || p.dow === dow)

/**
 * Whether `[fromMs, toMs)` holds any of the local hours an hour/day filter selects in `tz`. Every
 * UTC offset is a multiple of 15 minutes, so a local hour that overlaps the window either contains
 * its start or begins on a quarter-hour boundary inside it.
 */
export function windowHasHourDow(fromMs: number, toMs: number, tz: string, hour?: number | null, dow?: number | null): boolean {
  if (hour == null && dow == null) return true
  if (!(toMs > fromMs)) return false
  if (toMs - fromMs > 8 * DAY) return true
  if (matchesHourDow(zonedParts(fromMs, tz), hour, dow)) return true
  for (let t = Math.floor(fromMs / QUARTER_HOUR) * QUARTER_HOUR + QUARTER_HOUR; t < toMs; t += QUARTER_HOUR) {
    if (matchesHourDow(zonedParts(t, tz), hour, dow)) return true
  }
  return false
}

/**
 * The same hour/day selection expressed in another zone, or null when it cannot be: an hour in a
 * zone whose offset differs by a non-whole number of hours (Asia/Kolkata ↔ UTC) spans two hours
 * there, and a whole day in one zone is parts of two days in another. Resolved at the latest
 * occurrence at or before `refMs` (DST changes inside a long window are not reconciled).
 */
export function rezoneHourDow(
  sel: { hour?: number | null; dow?: number | null },
  fromTz: string,
  toTz: string,
  refMs: number,
): { hour?: number; dow?: number } | null {
  const hour = sel.hour ?? null
  const dow = sel.dow ?? null
  if (hour == null && dow == null) return {}
  const diff = tzOffsetMs(refMs, toTz) - tzOffsetMs(refMs, fromTz)
  if (fromTz === toTz || (diff === 0 && hour == null)) return { hour: hour ?? undefined, dow: dow ?? undefined }
  if (hour == null || diff % HOUR !== 0) return null
  // The start of the latest occurrence of the selected local hour (and day) at or before refMs.
  for (let t = Math.floor(refMs / QUARTER_HOUR) * QUARTER_HOUR, n = 0; n < 8 * 96; t -= QUARTER_HOUR, n++) {
    const p = zonedParts(t, fromTz)
    if (p.minute === 0 && matchesHourDow(p, hour, dow)) {
      const q = zonedParts(t, toTz)
      if (q.minute !== 0) return null
      return dow == null ? { hour: q.hour } : { hour: q.hour, dow: q.dow }
    }
  }
  return null
}
