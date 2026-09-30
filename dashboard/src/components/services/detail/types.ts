/**
 * Service detail shapes from the services-review contract. Every new field is
 * optional: older appliances omit them, and the UI must degrade to its
 * previous behaviour rather than inventing values.
 */
import type { ServiceCheck, ServiceMetricPoint, ServiceMetricResponse } from '@/types'

export type ServiceCheckExt = ServiceCheck & {
  owner_team?: string | null
  runbook_url?: string | null
  business_owner?: string | null
  criticality?: string | null
  sla_target_pct?: number | string | null
  stale?: boolean
  evidence_age_sec?: number | null
}

export type StatusHistoryEvent = {
  timestamp: string
  old_status: string | null
  new_status: string
  reason?: string | null
  duration_sec?: number | null
}

/** Raw probe or rollup bucket. Buckets carry sample/failed counts on newer appliances. */
export type MetricPoint = ServiceMetricPoint & {
  uptime_pct?: number | null
  sample_count?: number | null
  failed_count?: number | null
  p95_response_ms?: number | null
  max_response_ms?: number | null
}

export type PercentileBasis = 'exact_per_bucket' | 'per_probe' | 'unavailable'

export type MetricResponse = Omit<ServiceMetricResponse, 'points'> & {
  points: MetricPoint[]
  bucket_sec?: number | null
  truncated?: boolean
  total_samples?: number
  percentile_basis?: PercentileBasis
}

export type SlaIncident = {
  start: string
  end: string | null
  ongoing: boolean
  starts_before_window: boolean
  ends_after_window: boolean
  downtime_sec: number
  duration_sec: number | null
}

export type SlaStats = {
  availability_basis?: string
  probe_uptime_pct?: number | null
  probe_downtime_sec?: number
  probe_error_rate_pct?: number | null
  uptime_pct: number | null
  sample_count: number
  incident_count: number
  total_downtime_sec: number
  longest_incident_sec: number
  mttr_sec: number | null
  mtbf_sec: number | null
  avg_response_ms: number | null
  p95_response_ms: number | null
  max_response_ms: number | null
  error_rate_pct: number | null
  uptime_streak_sec: number | null
  uptime_streak_started_at?: string | null
  covered_sec?: number
  coverage_pct?: number
  unknown_sec?: number
  // Contract additions (optional on older appliances)
  requested_from?: string
  requested_to?: string
  effective_from?: string
  effective_to?: string
  requested_sec?: number
  maintenance_sec?: number
  latency_sample_count?: number
  min_response_ms?: number | null
  incidents?: SlaIncident[]
  incidents_truncated?: boolean
  completed_incident_count?: number
  mttr_population?: string
  ongoing_incident?: { start: string; duration_sec: number } | null
  target_pct?: number | null
  target_source?: 'service' | 'policy' | 'default'
  error_budget_sec?: number | null
  budget_used_sec?: number | null
  error_budget_used_pct?: number | null
  error_budget_remaining_sec?: number | null
  burn_rate?: number | null
  meets_target?: boolean | null
}

export type HourlyUptime = {
  ts: string
  uptime_pct: number | null
  sample_count: number
  covered_sec?: number
  maintenance_sec?: number
}

export type MaintenanceState = 'scheduled' | 'active' | 'ended' | 'cancelled' | 'expired'

export type MaintenanceWindow = {
  id: string
  scope_type: 'check' | 'group' | 'tag' | 'all'
  scope_label?: string | null
  scope_check_id?: string | null
  scope_group_id?: string | null
  scope_tag?: string | null
  starts_at: string
  ends_at: string
  effective_ends_at?: string | null
  ended_at?: string | null
  cancelled_at?: string | null
  state?: MaintenanceState
  reason?: string | null
  active: boolean
  inherited?: boolean
}

export type ServerIncident = {
  id: string
  kind: 'down' | 'warning'
  start: string
  end: string | null
  ongoing: boolean
  starts_before_window: boolean
  duration_sec: number | null
  in_window_sec: number
  reason_start?: string | null
  reason_end?: string | null
  severity_path?: string[]
}

export type IncidentsResponse = {
  data: ServerIncident[]
  meta: { total: number; skip: number; limit: number }
  entering_state?: string | null
  coverage_pct?: number | null
  sample_count?: number | null
  server_time?: string
}

/** One incident as the UI draws it, whether built on the server or in the browser. */
export type Outage = {
  id: string
  start: number
  /** The server's exact start string, when the incident came from the appliance. */
  startISO?: string
  end: number | null
  kind: 'down' | 'warn'
  reason?: string
  reasonEnd?: string
  clippedStart: boolean
  /** The incident continued past the window's end — `end` is the window edge, not a recovery. */
  clippedEnd?: boolean
  /** How the interval closed: recovery, missing evidence, severity change or window edge. */
  endKind?: 'recovered' | 'gap' | 'escalated' | 'window' | 'ongoing'
  severityPath?: string[]
  durationSec?: number | null
}

export type ServiceAlert = {
  id: string
  status: 'active' | 'acknowledged' | 'resolved' | 'pending' | string
  severity: string
  message: string
  triggered_at: string
  acknowledged_at?: string | null
  resolved_at?: string | null
  rule_id?: string | null
  metadata?: {
    is_recovery?: boolean
    old_status?: string
    new_status?: string
    error?: string | null
    suppressed_by_dependency?: boolean
  } | null
}

export type AlertsPage = { data: ServiceAlert[]; meta: { total: number; skip?: number; limit?: number } }

export type IncidentNote = {
  id: string
  incident_started_at: string | null
  assignee: string | null
  body: string
  author_name: string | null
  created_at: string
}

export type ConfigHistoryEntry = {
  id: string
  at: string
  actor: string | null
  action: string
  changes: Record<string, { old: unknown; new: unknown }> | null
  metadata?: Record<string, unknown> | null
}

export type SystemTime = {
  server_time: string
  timezone: string | null
  ntp_synchronized: boolean | null
  ntp_service: 'active' | 'inactive' | null
  source: string
}
