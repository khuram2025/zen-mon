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
  location_summary: { location: string; devices: number; down: number; availability_pct: number }[]
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

const STALE_MS = 30_000
const RETRY_COUNT = 1

/** Keep prior report data visible while the time window refetches. */
const keepPrev = <T,>(prev: T | undefined) => prev

/**
 * `useTimeRange()` recomputes `toISO` from `Date.now()` on every render. If we
 * key React Query directly on those raw ISO strings, every render mints a
 * brand-new key and triggers an immediate refetch. Round the key components
 * to the nearest minute to give us a stable bucket while still passing the
 * exact ISOs to the API.
 */
function bucketKey(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const bucketed = Math.floor(t / 60_000) * 60_000
  return new Date(bucketed).toISOString()
}

export function useExecutiveReport({ fromISO, toISO }: RangeArgs) {
  return useQuery<ExecutiveData>({
    queryKey: ['report', 'executive', bucketKey(fromISO), bucketKey(toISO)],
    queryFn: async () => (await api.get('/reports/data/executive', { params: { from: fromISO, to: toISO } })).data,
    staleTime: STALE_MS,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useTechnicalReport({ fromISO, toISO }: RangeArgs) {
  return useQuery<TechnicalData>({
    queryKey: ['report', 'technical', bucketKey(fromISO), bucketKey(toISO)],
    queryFn: async () => (await api.get('/reports/data/technical', { params: { from: fromISO, to: toISO } })).data,
    staleTime: STALE_MS,
    retry: RETRY_COUNT,
    placeholderData: keepPrev,
  })
}

export function useBusinessReport({ fromISO, toISO }: RangeArgs) {
  return useQuery<BusinessData>({
    queryKey: ['report', 'business', bucketKey(fromISO), bucketKey(toISO)],
    queryFn: async () => (await api.get('/reports/data/business', { params: { from: fromISO, to: toISO } })).data,
    staleTime: STALE_MS,
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
