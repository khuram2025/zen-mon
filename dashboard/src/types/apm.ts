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
  /** Registry tags (classification); editable via PATCH /apm/services/{name}/meta. */
  tags?: string[]
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

/* ── Browser Real User Monitoring ─────────────────────────────────────── */

export const RUM_RANGES = ['15m', '1h', '6h', '24h', '7d', '30d', '90d'] as const
export type RumRange = typeof RUM_RANGES[number]

export const RUM_TABS = ['overview', 'web-vitals', 'views', 'sessions', 'errors', 'resources', 'actions'] as const
export type RumTab = typeof RUM_TABS[number]

export interface RumFilters {
  application_id: string
  env: string
  view_name: string
  browser: string
  browser_version: string
  os: string
  device_type: string
  country: string
  service_version: string
  client_ip: string
}

export interface RumFacetValue {
  value: string
  count: number
}

export type RumFacets = Record<keyof RumFilters, RumFacetValue[]>

/** A percentile is null until at least one valid sample has been received. */
export interface RumVitalMetric {
  p75: number | null
  samples: number
  /** Percentage points in the inclusive 0–100 range. */
  good_pct?: number | null
  needs_improvement_pct?: number | null
  poor_pct?: number | null
}

export interface RumOverview {
  range: RumRange
  filters: Partial<RumFilters>
  totals: {
    events: number
    sessions: number
    views: number
    errors: number
    /** Errors from sessions included in the configured analytics sample. */
    sampled_errors?: number
    /** Errors retained for diagnostics even though their sessions were sampled out. */
    unsampled_errors?: number
    error_sessions: number
    resources: number
    actions: number
    long_tasks: number
    resource_failures?: number
  }
  /** Row counts of the explorer tabs (routes, sessions, error groups, resource groups, action groups). */
  explorer?: {
    views: number
    sessions: number
    errors: number
    resources: number
    actions: number
  }
  rates: {
    error_session_rate: number | null
    resource_failure_rate?: number | null
  }
  vitals: {
    lcp: RumVitalMetric
    inp: RumVitalMetric
    cls: RumVitalMetric
    fcp: RumVitalMetric
    ttfb: RumVitalMetric
    load: RumVitalMetric
  }
  ingest_health?: RumIngestHealth | string | null
  releases?: RumRelease[]
}

export interface RumRelease {
  service_version: string
  sessions: number
  views: number
  errors: number
  error_session_rate: number | null
  lcp_p75: number | null
  lcp_samples: number
  inp_p75: number | null
  inp_samples: number
  cls_p75: number | null
  cls_samples: number
  last_seen: string
}

export interface RumTimeseriesPoint {
  timestamp: string
  views: number
  sessions: number
  errors: number
  resources?: number
  resource_failures?: number
  actions?: number
  long_tasks?: number
  error_sessions?: number
  lcp_p75: number | null
  lcp_samples: number
  inp_p75: number | null
  inp_samples: number
  cls_p75: number | null
  cls_samples: number
  fcp_p75?: number | null
  fcp_samples?: number
  ttfb_p75?: number | null
  ttfb_samples?: number
  load_p75?: number | null
  load_samples?: number
}

export interface RumTimeseries {
  bucket_seconds: number
  series: RumTimeseriesPoint[]
}

export interface RumListResponse<T> {
  total: number
  page: number
  page_size: number
  items: T[]
  coverage?: RumCoverage
  sampling?: RumSamplingMetadata
}

export interface RumSamplingMetadata {
  includes_retained_unsampled_errors: boolean
  aggregate_error_session_rates_use_sampled_sessions_only: boolean
}

export interface RumCoverage {
  raw_retention_days: number
  rollup_retention_days: number
  partial: boolean
  message: string | null
}

export interface RumView {
  application_id: string
  env: string
  service_version?: string
  view_name: string
  url?: string
  views: number
  sessions: number
  errors: number
  error_count?: number
  error_sessions?: number
  error_session_rate: number | null
  lcp_p75: number | null
  lcp_samples: number
  inp_p75: number | null
  inp_samples: number
  cls_p75: number | null
  cls_samples: number
  fcp_p75: number | null
  fcp_samples: number
  ttfb_p75: number | null
  ttfb_samples: number
  load_p75: number | null
  load_samples: number
  last_seen: string | null
  backend_trace_id?: string
  backend_trace_ids?: string[]
}

export interface RumSession {
  session_id: string
  application_id: string
  env: string
  service_version?: string
  sdk_version?: string
  sampled?: boolean
  user_id?: string
  started_at: string
  last_seen: string
  duration_ms: number
  views: number
  actions: number
  resources: number
  long_tasks: number
  errors: number
  browser: string
  browser_version?: string
  os?: string
  device_type: string
  country: string
  client_ip?: string
  backend_trace_id?: string
  backend_trace_ids?: string[]
  connection_type?: string
  connection_rtt_ms?: number | null
  connection_downlink?: number | null
  language?: string
  timezone?: string
  screen_res?: string
  viewport?: string
}

export interface RumError {
  fingerprint: string
  message: string
  error_type?: string
  source?: string
  stack?: string
  application_id: string
  env: string
  service_version?: string
  view_name: string
  browser?: string
  browser_version?: string
  os?: string
  device_type?: string
  country?: string
  count: number
  sampled_count?: number
  unsampled_count?: number
  sessions: number
  first_seen: string
  last_seen: string
  backend_trace_id?: string
  backend_trace_ids?: string[]
}

export interface RumResource {
  name: string
  url?: string
  resource_type: string
  method?: string
  status_code?: number | null
  application_id: string
  env: string
  service_version?: string
  view_name: string
  count: number
  failed_count: number
  duration_p75: number | null
  duration_samples?: number
  size_avg: number | null
  size_samples?: number
  failure_rate?: number | null
  last_seen: string
  backend_trace_id?: string
  backend_trace_ids?: string[]
  dns_p75?: number | null
  connect_p75?: number | null
  tls_p75?: number | null
  wait_p75?: number | null
  download_p75?: number | null
  timing_samples?: number
  server_p75?: number | null
  db_p75?: number | null
  server_samples?: number
  protocol?: string
  /** SDK versions that reported this resource; phase timing needs 2.1+. */
  sdk_versions?: string[]
  /** Timing-capable samples whose phases were all zero: cross-origin without Timing-Allow-Origin. */
  opaque_samples?: number
  /** Execution split averaged over the correlated APM traces, when no Server-Timing was sent. */
  backend?: (RumBackendTiming & { traces: number }) | null
}

export interface RumAction {
  name: string
  action_type: string
  target?: string
  application_id: string
  env: string
  service_version?: string
  view_name: string
  count: number
  error_count: number
  duration_p75: number | null
  duration_samples?: number
  last_seen: string
  backend_trace_id?: string
  backend_trace_ids?: string[]
}

export type RumEventType = 'view' | 'action' | 'error' | 'resource' | 'long_task'

export interface RumTimelineEvent {
  timestamp: string
  event_id?: string
  event_type: RumEventType
  view_id?: string
  view_name: string
  url?: string
  page_url?: string
  name?: string
  action_name?: string
  action_type?: string
  target?: string
  resource_url?: string
  resource_type?: string
  method?: string
  status_code?: number | null
  duration_ms?: number | null
  size_bytes?: number | null
  transfer_size?: number | null
  encoded_body_size?: number | null
  error_message?: string
  error_type?: string
  stack?: string
  error_stack?: string
  source?: string
  error_source?: string
  error_fingerprint?: string
  lcp?: number | null
  inp?: number | null
  cls?: number | null
  fcp?: number | null
  ttfb?: number | null
  load_ms?: number | null
  vitals?: Partial<Record<'lcp' | 'inp' | 'cls' | 'fcp' | 'ttfb' | 'load', number | null>>
  backend_trace_id?: string
  attributes?: Record<string, string>
  service_version?: string
  browser_version?: string
  os?: string
  sdk_version?: string
  is_final?: boolean
  sampled?: boolean
  end_reason?: string
  vital_attribution?: Record<string, string>
  timing?: RumRequestTiming | null
  backend?: RumBackendTiming | null
}

/** Navigation/Resource Timing phase split for one request (milliseconds). */
export interface RumRequestTiming {
  redirect_ms: number
  dns_ms: number
  connect_ms: number
  tls_ms: number
  wait_ms: number
  download_ms: number
  blocked_ms: number
  processing_ms: number
  server_ms: number
  db_ms: number
  has_server_timing: boolean
  protocol?: string
}

/** Execution split of a correlated backend APM trace. */
export interface RumBackendTiming {
  server_ms: number
  db_ms: number
  db_calls: number
  spans: number
  service?: string
  db_systems?: string[]
  has_error?: boolean
}

export interface RumBackendSummary {
  traces: number
  services: string[]
  db_systems: string[]
  avg_server_ms: number | null
  avg_db_ms: number | null
}

export interface RumBreakdownSide {
  samples: number
  phases: Record<'redirect' | 'dns' | 'connect' | 'tls' | 'wait' | 'download' | 'blocked' | 'processing', number | null>
  server_p75: number | null
  db_p75: number | null
  server_samples: number
  duration_p75: number | null
  duration_samples: number
}

export interface RumBreakdown {
  range: RumRange
  coverage?: RumCoverage
  page_loads: RumBreakdownSide
  api_requests: RumBreakdownSide
  slowest_endpoints: Array<{
    url: string
    method?: string
    count: number
    duration_p75: number | null
    wait_p75: number | null
    server_p75: number | null
    db_p75: number | null
    server_samples: number
    /** Where the app/db split came from: the Server-Timing header or correlated APM traces. */
    server_source?: 'server-timing' | 'trace' | null
    failures: number
  }>
}

export interface RumSessionDetail {
  session: RumSession
  backend_summary?: RumBackendSummary | null
  timeline: RumTimelineEvent[]
  total?: number
  page?: number
  page_size?: number
  coverage?: RumCoverage
}

export interface RumIngestHealth {
  status?: 'healthy' | 'degraded' | 'critical' | 'no_data' | string
  accepted?: number
  accepted_since_process_start?: number
  rejected?: number
  rate_limited?: number
  duplicates?: number
  dropped?: number
  sampled_out?: number
  storage_errors?: number
  last_event_at?: string | null
  sdk_version?: string | null
  sdk_versions?: string[]
  issues?: string[]
}
