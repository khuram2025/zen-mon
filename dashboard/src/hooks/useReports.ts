import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

interface RangeArgs {
  fromISO: string
  toISO: string
}

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

/* Fields marked "newer backends" arrive with the availability rework
 * (feat/availability-management). Older appliances omit them, so every
 * consumer must treat them as possibly missing. A `null` percentage means
 * NOT MEASURED — never zero. */

/** A sub-query that failed; the response is partial when non-empty. */
export interface ReportDataError {
  source: string
  message: string
}

export interface StatusNow {
  up: number
  down: number
  degraded: number
  maintenance: number
  unknown: number
  total: number
}

/** Device breakdown computed with the headline's bounds and denominator. */
export interface DeviceSegmentSummary {
  name: string
  devices: number
  up_now: number
  down_now: number
  availability_pct: number | null
  measured: number
  /** Confirmed outage minutes inside the window (newer backends). */
  downtime_minutes?: number | null
  outages?: number | null
}

/** Per availability-trend bucket, over every confirmed outage episode. */
export interface OutageActivityPoint {
  ts: string
  outages_started: number
  entities_down: number
  downtime_minutes: number
}

export interface DowntimeByDevice {
  device_id: string
  hostname: string
  location?: string | null
  group_name?: string | null
  device_type?: string | null
  downtime_minutes: number | null
  outages: number
  ongoing: boolean
  availability_pct: number | null
}

export interface RecoveryBin { key: string; label: string; count: number }

export type ComplianceState = 'met' | 'at_risk' | 'breached' | 'not_assessed'

export interface ExecutiveData {
  from: string
  to: string
  /** Server time of the response (newer backends). */
  generated_at?: string
  data_errors?: ReportDataError[]
  /** Span the figures were actually measured over. Shorter than [from, to]
   *  when retention or a collection gap means the window is only partly
   *  covered — a 30-day request on an appliance with eight days of samples
   *  still returns eight days of numbers. */
  coverage?: {
    source_table: string | null
    from: string | null
    to: string | null
    measured_entities?: number | null
    expected_entities?: number | null
    /** Share of the window between first and last sample — not a guarantee
     *  of continuous collection. */
    covered_pct?: number | null
    last_observation?: string | null
  } | null
  kpis: {
    availability_pct: number | null
    availability_delta_pct: number | null
    active_critical_count: number
    mttr_minutes: number | null
    mttr_delta_minutes: number | null
    devices_monitored: number
    sla_target_pct: number
    sla_attained_pct: number | null
    /** Confirmed device outage episodes (newer backends); alert count before. */
    incidents_count: number
    incidents_delta: number | null
    /** Device-minutes of planned maintenance excluded from availability. */
    maintenance_minutes?: number
    configured_target_pct?: number | null
    target_source?: 'policy' | 'default' | null
    sla_met?: boolean | null
    availability_delta_pp?: number | null
    outage_episodes?: number | null
    ongoing_outages?: number | null
    outage_downtime_minutes?: number | null
    alerts_count?: number | null
    alert_mttr_minutes?: number | null
    active_critical_now?: number | null
    critical_raised_in_period?: number | null
    devices_expected?: number | null
    devices_measured?: number | null
  }
  availability_trend: { ts: string; availability_pct: number | null }[]
  top_issues: {
    device_id: string
    hostname: string
    issue: string
    duration_minutes: number | null
    alert_count: number
    severity: string
    downtime_minutes?: number | null
    outages?: number | null
    category?: 'outage' | 'degraded' | 'alert_noise' | null
    availability_pct?: number | null
  }[]
  /** `availability_pct` is measured over the selected window (maintenance
   *  excluded) and is null when the location reported no samples in it;
   *  `devices`/`down` are live counts. */
  location_summary: {
    location: string
    devices: number
    down: number
    availability_pct: number | null
    up_now?: number | null
    measured?: number | null
    downtime_minutes?: number | null
    outages?: number | null
  }[]
  group_summary?: DeviceSegmentSummary[] | null
  type_summary?: DeviceSegmentSummary[] | null
  outage_timeline: {
    device_id: string
    hostname: string
    started_at: string | null
    ended_at?: string | null
    ongoing?: boolean
    /** Full episode length (to now when ongoing); null only when genuinely
     *  unknown (newer backends). */
    duration_minutes: number | null
    /** Part of the episode inside the selected window (newer backends). */
    in_window_minutes?: number | null
    /** false when the log opens on a recovery: started_at is the clipped
     *  window start and the real start is unknown. */
    start_known?: boolean
    kind: string
  }[]
  status_now?: StatusNow | null
  /** Newer backends: aligned with availability_trend, over every episode. */
  outage_activity?: OutageActivityPoint[] | null
  downtime_by_device?: DowntimeByDevice[] | null
  recovery_histogram?: RecoveryBin[] | null
}

export interface TechnicalData {
  from: string
  to: string
  generated_at?: string
  data_errors?: ReportDataError[]
  worst_devices: {
    device_id: string
    hostname: string
    ip: string
    availability_pct: number
    outage_count: number
    avg_rtt_ms: number | null
    p95_rtt_ms: number | null
  }[]
  noisy_alerts: {
    rule_key: string
    alert_count: number
    device_id: string | null
    hostname: string | null
    sample_message: string | null
    severity: string | null
  }[]
  top_bandwidth_interfaces: {
    device_id: string
    hostname: string
    if_index: number
    if_name: string
    in_bps_avg: number
    out_bps_avg: number
    utilization_pct: number | null
  }[]
  alert_volume_by_severity: { ts: string; critical: number; warning: number; info: number }[]
  interface_errors: {
    device_id: string
    hostname: string
    if_index: number
    if_name: string
    oper_status: string
  }[]
  outage_history: {
    device_id: string
    hostname: string
    started_at: string | null
    ended_at?: string | null
    ongoing?: boolean
    duration_minutes: number | null
    in_window_minutes?: number | null
    start_known?: boolean
    kind: string
  }[]
}

export interface BusinessData {
  from: string
  to: string
  generated_at?: string
  data_errors?: ReportDataError[]
  /** "confirmed" on newer backends. */
  availability_basis?: string | null
  service_availability: {
    service_check_id: string
    name: string
    type: string
    group_name: string
    status: string
    /** Confirmed-incident basis on newer backends; raw probe ratio before. */
    availability_pct: number | null
    checks_total: number
    checks_failed: number
    probe_availability_pct?: number | null
    coverage_pct?: number | null
    downtime_seconds?: number | null
    outages?: number | null
    target_pct?: number | null
    target_source?: 'service' | 'policy' | 'default' | null
    meets_target?: boolean | null
    compliance?: ComplianceState | null
    error_budget_used_pct?: number | null
    criticality?: 'critical' | 'high' | 'medium' | 'low' | null
    business_owner?: string | null
    grouped?: boolean | null
    availability_basis?: string | null
  }[]
  response_time_quantiles: {
    service_check_id: string
    name: string
    p50_ms: number | null
    p95_ms: number | null
    p99_ms: number | null
  }[]
  tls_warnings: {
    service_check_id: string
    name: string
    tls_expiry_date: string | null
    days_remaining: number
    severity: string
    warn_days?: number | null
    critical_days?: number | null
  }[]
  /** null when no service has a business mapping (newer backends). */
  customer_impact_minutes: number | null
  service_outages: {
    service_check_id: string
    name: string
    group_name?: string | null
    started_at: string | null
    ended_at?: string | null
    ongoing?: boolean
    duration_minutes: number | null
  }[]
  compliance_summary?: {
    met: number
    at_risk: number
    breached: number
    not_assessed: number
    eligible: number
  } | null
  impact_assessed_services?: number | null
  impact_unassessed_services?: number | null
  impact_basis?: 'confirmed_downtime_merged' | null
  confirmed_downtime_minutes?: number | null
  tls_evidence?: { eligible: number; with_expiry_data: number } | null
  status_now?: StatusNow | null
}

export interface InventoryData {
  as_of: string
  devices_by_type: { type: string; count: number }[]
  devices_by_vendor: { vendor: string; count: number }[]
  devices_by_location: { location: string; count: number }[]
  interface_totals: { total: number; monitored: number; down: number }
  sensors: {
    sensor_id: string
    name: string
    status: string
    last_heartbeat: string | null
    queue_depth: number
    queue_dropped_count: number
    version: string | null
    hostname: string | null
    site: string | null
  }[]
  recently_added_devices: {
    device_id: string
    hostname: string
    ip: string
    device_type: string
    location: string | null
    vendor: string | null
    model: string | null
    added_at: string | null
  }[]
  totals: { devices: number; sensors: number; interfaces: number }
}

/* ------------------------------------------------------------------ */
/*  Hooks                                                              */
/* ------------------------------------------------------------------ */

const RETRY_COUNT = 1

interface ReportQueryOptions {
  /** Skip fetching (e.g. a panel that is not on screen). Defaults to true. */
  enabled?: boolean
}

/** Keep prior report data visible while the time window refetches. */
const keepPrev = <T,>(prev: T | undefined) => prev

/**
 * `useTimeRange()` slides preset windows with the wall clock, re-deriving
 * `fromISO`/`toISO` every minute. Keying React Query on those raw ISO strings
 * mints a brand-new key on every tick, so all three (expensive) reports
 * refetch from scratch once a minute — and on a 7d/30d window a fresh minute
 * of samples cannot move the numbers.
 *
 * Bucket the key to a granularity proportional to the window instead: short
 * windows still feel live, long windows refresh at a rate that matches how
 * fast they can actually change. The exact ISOs are still sent to the API.
 */
function bucketMs(fromISO: string, toISO: string): number {
  const span = Date.parse(toISO) - Date.parse(fromISO)
  if (!Number.isFinite(span)) return 60_000
  if (span <= 2 * 3_600_000) return 60_000        // <= 2h  → 1 min
  if (span <= 24 * 3_600_000) return 5 * 60_000   // <= 24h → 5 min
  if (span <= 7 * 24 * 3_600_000) return 15 * 60_000  // <= 7d → 15 min
  return 30 * 60_000                              // longer → 30 min
}

function bucketKey(iso: string, ms: number): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(Math.floor(t / ms) * ms).toISOString()
}

/** Stable query key + a matching staleTime, so the data stays fresh for
 *  exactly as long as the key it is stored under. */
function rangeKey({ fromISO, toISO }: RangeArgs) {
  const ms = bucketMs(fromISO, toISO)
  return { key: [bucketKey(fromISO, ms), bucketKey(toISO, ms)], staleMs: ms }
}

export function useExecutiveReport({ fromISO, toISO }: RangeArgs, opts: ReportQueryOptions = {}) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<ExecutiveData>({
    enabled: opts.enabled ?? true,
    queryKey: ['report', 'executive', ...key],
    queryFn: async () => (await api.get('/reports/data/executive', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useTechnicalReport({ fromISO, toISO }: RangeArgs, opts: ReportQueryOptions = {}) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<TechnicalData>({
    enabled: opts.enabled ?? true,
    queryKey: ['report', 'technical', ...key],
    queryFn: async () => (await api.get('/reports/data/technical', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useBusinessReport({ fromISO, toISO }: RangeArgs, opts: ReportQueryOptions = {}) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<BusinessData>({
    enabled: opts.enabled ?? true,
    queryKey: ['report', 'business', ...key],
    queryFn: async () => (await api.get('/reports/data/business', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export interface AvailabilityPolicy {
  default_target_pct: number
  source?: string | null
}

/** Appliance-wide default availability target. 404 on older backends. */
export function useAvailabilityPolicy(enabled = true) {
  return useQuery<AvailabilityPolicy>({
    queryKey: ['availability-policy'],
    queryFn: async () => (await api.get('/reports/availability-policy')).data,
    staleTime: 5 * 60_000,
    retry: false,
    enabled,
  })
}

export function useInventoryReport() {
  return useQuery<InventoryData>({
    queryKey: ['report', 'inventory'],
    queryFn: async () => (await api.get('/reports/data/inventory')).data,
    staleTime: 60_000,
    retry: RETRY_COUNT,
  })
}
