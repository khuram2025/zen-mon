// Shared APM API types. Each page used to redeclare its own slice of these,
// which is how the service-map edge kept a `p95_ms` field the API had stopped
// sending. One declaration per response shape, imported everywhere.

export type Health = 'healthy' | 'degraded' | 'critical' | 'no_data'

export interface ServiceRED {
  name: string
  envs: string[]
  health: string
  request_count: number
  rps: number
  error_rate: number
  p50_ms: number
  p95_ms: number
  p99_ms: number
  apdex: number
}

export interface ServiceListResponse {
  services: ServiceRED[]
  facets: { env: Record<string, number>; health: Record<string, number> }
  window_seconds: number
}

export interface REDPoint {
  timestamp: string
  rps: number
  error_rate: number
  p50_ms: number
  p95_ms: number
}

export interface OperationRED {
  operation: string
  request_count: number
  rps: number
  error_rate: number
  p95_ms: number
}

export interface MapNode {
  name: string
  health: string
  rps: number
  error_rate: number
  p95_ms: number
}

/** Per-edge latency is a mean: the graph rollup stores sum+count, not a digest. */
export interface MapEdge {
  client: string
  server: string
  calls: number
  error_rate: number
  avg_ms: number
}

export interface ServiceMap {
  nodes: MapNode[]
  edges: MapEdge[]
}

export interface ErrorIssue {
  group_id: string
  exception_type: string
  message: string
  service: string
  services: string[]
  occurrences: number
  traces: number
  first_seen: string
  last_seen: string
  versions: string[]
  http_route: string
  status: string
  assignee: string | null
  resolved_in_version: string | null
}

export interface ErrorListResponse {
  issues: ErrorIssue[]
  /** Every status key is always present (zero included), plus `all`. */
  counts: Record<string, number>
}

export interface TraceSummary {
  trace_id: string
  root_service: string
  root_operation: string
  start_time: string
  duration_ms: number
  span_count: number
  error_count: number
  has_error: boolean
  services: string[]
}

export interface SloBurnTier {
  tier: string
  long_window_s: number
  short_window_s: number
  /** What the rollup granularity actually allowed us to measure. */
  long_window_effective_s: number
  short_window_effective_s: number
  long_requests: number
  short_requests: number
  factor: number
  severity: string
  long_burn: number | null
  short_burn: number | null
  breaching: boolean
}

export interface Slo {
  id: string
  name: string
  service_id: string
  service_name: string
  env: string | null
  operation: string | null
  sli_type: string
  latency_threshold_ms: number | null
  target: number
  window_days: number
  burn_alert_enabled: boolean
  notify_channels: string[]
}

export interface SloBudget {
  slo: Slo
  budget_fraction: number
  window_days: number
  window_requests: number
  budget_consumed: number | null
  budget_remaining: number | null
  tiers: SloBurnTier[]
}

export interface SyntheticMonitor {
  id: string
  name: string
  steps: unknown[]
  enabled: boolean
  status: string | null
  check_interval: number
  runs: number
  uptime_pct: number | null
  avg_ms: number | null
  last_run_at: string | null
}

export interface DataQuality {
  ingest: {
    accepted: number
    rejected: number
    dropped: number
    skewed: number
    flushes: number
    reject_rate: number
    queue_depth: number | null
    series: { t: string; accepted: number; rejected: number; dropped: number }[]
  }
  services: {
    name: string
    health: string
    last_seen_at: string | null
    silent_for_s: number | null
    reporting: boolean
  }[]
  agent_forwarders: Record<string, unknown>[]
  health: string
  issues: string[]
}
