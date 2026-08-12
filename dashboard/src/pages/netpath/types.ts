export type ProbeStatus = 'ok' | 'degraded' | 'down' | 'unreached' | 'pending'

export interface Probe {
  id: string
  name: string
  target_host: string
  target_ip: string | null
  target_port: number | null
  protocol: 'icmp' | 'tcp' | 'udp'
  max_hops: number
  probes_per_hop: number
  flows: number
  interval_s: number
  enabled: boolean
  run_now?: boolean
  internal_cidrs: string[]
  rtt_warn_ms: number
  rtt_crit_ms: number
  loss_warn_pct: number
  loss_crit_pct: number
  description: string
  tags: string[]
  last_run_at: string | null
  last_status: ProbeStatus | null
  last_rtt_ms: number | null
  last_loss_pct: number | null
  last_hop_count: number | null
  last_num_paths: number | null
  last_reached: boolean | null
  last_error: string | null
  created_at?: string
  updated_at?: string
  distinct_paths?: number
  latest_snapshot?: SnapshotSummary
}

export interface SnapshotSummary {
  id: number
  run_at: string
  status: ProbeStatus
  reached: boolean
  path_changed: boolean
  rtt_ms: number | null
  loss_pct: number | null
  worst_hop_loss_pct: number | null
  jitter_ms: number | null
  hop_count: number
  num_paths: number
  duration_ms: number | null
}

export interface HopNode {
  ip: string
  rtt_avg: number
  rtt_min: number
  rtt_max: number
  loss_pct: number
  sent: number
  recv: number
  is_dest: boolean
  flow_count: number
  hostname: string | null
  asn: number | null
  as_name: string | null
  country: string | null
  device_id: string | null
  device_name: string | null
  device_type: string | null
  is_internal: boolean
}

export interface Hop {
  ttl: number
  anonymous: boolean
  nodes: HopNode[]
}

export interface PathEdge {
  from_ip: string
  to_ip: string
  from_ttl: number
  to_ttl: number
  flows: number
  gap: boolean
}

export interface AsGroup {
  asn: number
  as_name: string | null
  count: number
  is_internal: boolean
}

export interface PathGraphData {
  probe: Probe
  snapshot: (SnapshotSummary & { protocol: string; path_hash: number | null }) | null
  target: { host: string; ip: string | null; port: number | null; reached: boolean }
  hops: Hop[]
  edges: PathEdge[]
  as_groups: AsGroup[]
}

export interface HopLadderRow {
  ttl: number
  ip: string | null
  is_dest: boolean
  series: ({ rtt: number | null; loss: number | null; anon: boolean; ip: string | null } | null)[]
}

export interface NetPathEvent {
  id: number
  event_type: string
  snapshot_id: number | null
  severity: string
  details: Record<string, any>
  created_at: string
}

export interface DistinctPath {
  id: string
  path_hash: number
  hop_count: number
  hop_ips: string[]
  label: string | null
  first_seen: string
  last_seen: string
  seen_count: number
  as_path: { asn: number; as_name: string | null }[]
}

export interface Summary {
  total_probes: number
  enabled: number
  by_status: Record<string, number>
  ok: number
  degraded: number
  unreachable: number
  path_changes_24h: number
  recent_events: (NetPathEvent & { probe_id: string; probe_name: string })[]
}

export interface CompareResult {
  a: { id: number; run_at: string; rtt_ms: number | null; loss_pct: number | null; hop_count: number }
  b: { id: number; run_at: string; rtt_ms: number | null; loss_pct: number | null; hop_count: number }
  rows: {
    ttl: number
    status: 'same' | 'added' | 'removed' | 'changed'
    same: HopLabel[]
    added: HopLabel[]
    removed: HopLabel[]
  }[]
  summary: {
    hops_added: HopLabel[]
    hops_removed: HopLabel[]
    identical: boolean
    rtt_delta: number
  }
}

export interface HopLabel {
  ip: string
  hostname: string | null
  asn: number | null
  as_name: string | null
  device_name: string | null
}
