import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

interface RangeArgs {
  fromISO: string
  toISO: string
}

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface ExecutiveData {
  from: string
  to: string
  /** Span the figures were actually measured over. Shorter than [from, to]
   *  when retention or a collection gap means the window is only partly
   *  covered — a 30-day request on an appliance with eight days of samples
   *  still returns eight days of numbers. */
  coverage?: {
    source_table: string | null
    from: string | null
    to: string | null
  }
  kpis: {
    availability_pct: number | null
    availability_delta_pct: number | null
    active_critical_count: number
    mttr_minutes: number | null
    mttr_delta_minutes: number | null
    devices_monitored: number
    sla_target_pct: number
    sla_attained_pct: number
    incidents_count: number
    incidents_delta: number
    /** Device-minutes of planned maintenance excluded from availability. */
    maintenance_minutes?: number
  }
  availability_trend: { ts: string; availability_pct: number | null }[]
  top_issues: {
    device_id: string
    hostname: string
    issue: string
    duration_minutes: number
    alert_count: number
    severity: string
  }[]
  /** `availability_pct` is measured over the selected window (maintenance
   *  excluded) and is null when the location reported no samples in it;
   *  `devices`/`down` are live counts. */
  location_summary: { location: string; devices: number; down: number; availability_pct: number | null }[]
  outage_timeline: {
    device_id: string
    hostname: string
    started_at: string | null
    duration_minutes: number
    kind: string
  }[]
}

export interface TechnicalData {
  from: string
  to: string
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
    duration_minutes: number
    kind: string
  }[]
}

export interface BusinessData {
  from: string
  to: string
  service_availability: {
    service_check_id: string
    name: string
    type: string
    group_name: string
    status: string
    availability_pct: number | null
    checks_total: number
    checks_failed: number
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
  }[]
  customer_impact_minutes: number
  service_outages: {
    service_check_id: string
    name: string
    started_at: string | null
    duration_minutes: number
  }[]
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

export function useExecutiveReport({ fromISO, toISO }: RangeArgs) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<ExecutiveData>({
    queryKey: ['report', 'executive', ...key],
    queryFn: async () => (await api.get('/reports/data/executive', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useTechnicalReport({ fromISO, toISO }: RangeArgs) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<TechnicalData>({
    queryKey: ['report', 'technical', ...key],
    queryFn: async () => (await api.get('/reports/data/technical', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useBusinessReport({ fromISO, toISO }: RangeArgs) {
  const { key, staleMs } = rangeKey({ fromISO, toISO })
  return useQuery<BusinessData>({
    queryKey: ['report', 'business', ...key],
    queryFn: async () => (await api.get('/reports/data/business', { params: { from: fromISO, to: toISO } })).data,
    staleTime: staleMs,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
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
