/**
 * Response shapes of the breakdown endpoints (server/app/api/v1/netflow/breakdowns.py) as the
 * NetFlow pages request them: `with_totals=1` inside the `envelope=1` wrapper.
 *
 * `total` is the whole filtered scope (the denominator), not the sum of the rows returned;
 * `additive` says whether the items partition it (sent bytes do; endpoint involvement, interface
 * volumes and per-side country/AS attribution do not); `remainder` is what the returned items
 * leave of the scope. `null` always means unknown, never zero.
 */

import type { DeviceStatusPayload } from '../overviewModel'

export interface Totals {
  bytes: number
  packets: number
  flows: number
}

export type RankBy = 'src' | 'dst' | 'total'
export type RankMetric = 'bytes' | 'packets' | 'flows'

export interface Paged<T> {
  items: T[]
  total: Totals | null
  distinct: number | null
  limit: number
  offset: number
  basis: string
  additive: boolean
  remainder: Partial<Totals> | null
}

export interface HostEnrichment {
  group?: string | null
  country?: string | null
  country_name?: string | null
  asn?: number | null
  as_name?: string | null
  hostname?: string | null
  hostname_pending?: boolean
  hostname_resolved_at?: string | null
}

export interface ServiceInfo {
  service_port: number | null
  icmp_type_code?: number
  service: string | null
  service_note: string | null
  port_applicable: boolean
  /** Filter keys that select exactly this service (protocol + service_port / dst_port). */
  drill: { protocol: number; service_port?: number; dst_port?: number }
}

export interface HostService extends ServiceInfo {
  protocol: number
  application: string
  bytes: number
  packets: number
  flows: number
}

export interface Talker extends HostEnrichment {
  ip: string
  bytes: number
  packets: number
  flows: number
  src_bytes: number
  src_packets: number
  src_flows: number
  dst_bytes: number
  dst_packets: number
  dst_flows: number
  rank_by: RankBy
  rank_metric: RankMetric
  rank_value: number
  share_pct?: number | null
  first_seen: string | null
  last_seen: string | null
  exporters: { ip: string }[]
  protocols: { protocol: number; name: string }[]
  services: HostService[]
}

export interface TalkersPage extends Paged<Talker> {
  rank_by: RankBy
  rank_metric: RankMetric
}

export interface InterfaceRef {
  exporter_ip: string
  ifindex: number
  /** `N@exporter`, the value of the `iface` filter for exactly this interface. */
  iface: string
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_speed: number | null
  device_hostname: string | null
  display_name: string
}

export type Orientation = 'client_server' | 'address_order' | 'mixed'
export type Confidence = 'high' | 'medium' | 'low' | 'n/a'

export interface Classification {
  application: string
  category: string
  confidence: Confidence
  basis: string
  basis_text: string
  ip_protocol: string
  visibility_note: string | null
}

export interface Conversation extends ServiceInfo, Classification {
  src: string
  dst: string
  client: string
  server: string
  orientation: Orientation
  protocol: number
  protocol_name: string
  dst_port: number
  port_class: string
  bytes: number
  packets: number
  flows: number
  bytes_c2s: number
  bytes_s2c: number
  packets_c2s: number
  packets_s2c: number
  flows_c2s: number
  flows_s2c: number
  share_pct?: number | null
  client_ports: number[]
  exporters: { ip: string; hostname: string | null }[]
  input_interfaces: InterfaceRef[]
  output_interfaces: InterfaceRef[]
  client_side_interfaces: InterfaceRef[]
  server_side_interfaces: InterfaceRef[]
  first_seen: string | null
  last_seen: string | null
  received_at: string | null
  avg_duration_ms: number | null
  duration_valid_flows: number
  tcp_flags: number | null
  tcp_flags_known: boolean
  flagged_flows: number
  avg_bytes: number | null
  avg_packets: number | null
  client_hostname?: string | null
  client_hostname_resolved_at?: string | null
  client_hostname_pending?: boolean
  client_country?: string | null
  client_group?: string | null
  server_hostname?: string | null
  server_hostname_resolved_at?: string | null
  server_hostname_pending?: boolean
  server_country?: string | null
  server_group?: string | null
}

export interface ServiceRow extends ServiceInfo, Classification {
  protocol: number
  protocol_name: string
  port: number
  bytes: number
  packets: number
  flows: number
  request_bytes: number
  response_bytes: number
  share_pct?: number | null
}

export interface ProtocolRow {
  protocol: number
  name: string
  bytes: number
  packets: number
  flows: number
  request_bytes: number
  response_bytes: number
  first_seen: string | null
  last_seen: string | null
  port_applicable: boolean
  services: ServiceRow[]
  share_pct?: number | null
}

export interface ApplicationService extends ServiceInfo {
  protocol: number
  protocol_name: string
  port: number
  bytes: number
  packets: number
  flows: number
  request_bytes: number
  response_bytes: number
  confidence: Confidence
  basis: string
  basis_text: string
}

export interface ApplicationRow {
  name: string
  category: string
  bytes: number
  packets: number
  flows: number
  request_bytes: number
  response_bytes: number
  confidence_bytes: Record<Confidence, number>
  confidence: Confidence
  selected: boolean
  visibility_note: string | null
  services: ApplicationService[]
  share_pct?: number | null
}

export interface InterfaceRow extends InterfaceRef {
  device_id: string | null
  role: string | null
  in_bytes: number
  out_bytes: number
  bytes: number
  in_packets: number
  out_packets: number
  packets: number
  in_flows: number
  out_flows: number
  flows: number
  in_bps_avg: number
  out_bps_avg: number
  speed_in_bps: number | null
  speed_out_bps: number | null
  speed_source: string
  selected: boolean
  /** The selected interface, appended because it is not among the ranked rows of this page. */
  pinned?: boolean
}

export interface InterfacesPage extends Paged<InterfaceRow> {
  scope: { interface_excluded: boolean; selected: string | null; note: string | null }
  /**
   * No utilization percentage here: this ranking counts records the page's way (count=all adds an
   * exporter's ingress and egress copies) and averages over the whole window (gaps read as idle).
   * Utilization is the Capacity page's (one definition); rows link there.
   */
  utilization?: { source: 'capacity'; path: string; note: string }
  window_seconds: number
}

export interface DscpRow {
  dscp: number
  label: string
  bytes: number
  packets: number
  flows: number
  selected: boolean
  share_pct?: number | null
}

/** `/netflow/dscp?with_totals=1`: records without an exported ToS field are a bucket of their own. */
export interface DscpPage extends Paged<DscpRow> {
  not_exported?: { bytes: number; packets: number; flows: number; share_pct?: number | null } | null
  /** Share of the bytes whose DSCP is known (null when there is no traffic). */
  tos_coverage_pct?: number | null
}

export type FlagSupport = 'supported' | 'partial' | 'unsupported' | 'no_tcp' | 'not_applicable'

export interface TcpFlags {
  total_tcp: number
  syn_only: number
  ack_only: number
  rst: number
  fin: number
  psh: number
  urg: number
  no_flags: number
  flagged_tcp: number
  unknown_tcp: number
  legacy_tcp: number
  total_bytes: number
  flagged_bytes: number
  flag_coverage_pct: number | null
  /** The one RST share (same definition as /device-status): null with a status when not stated. */
  rst_ratio_pct?: number | null
  rst_ratio?: DeviceStatusPayload['rst']
  support: FlagSupport
  reason: string | null
}

export interface SideRow {
  name: string
  src_bytes: number
  src_packets: number
  src_flows: number
  dst_bytes: number
  dst_packets: number
  dst_flows: number
  bytes: number
  packets: number
  flows: number
  src_share_pct: number | null
  dst_share_pct: number | null
  selected: boolean
}

export type NetClassRow = SideRow

export interface NetClassesPage extends Paged<NetClassRow> {
  pairs: { src: string; dst: string; bytes: number; packets: number; flows: number }[]
}

export interface GeoStatus {
  country_db: boolean
  asn_db: boolean
  country_db_version: string | null
  asn_db_version: string | null
}

export type PlaceKind = 'country' | 'private' | 'reserved' | 'unknown' | 'asn'

export interface CountryRow extends SideRow {
  country: string | null
  country_name: string
  kind: PlaceKind
  hint?: string
}

export interface SideRemainder {
  src_bytes: number
  dst_bytes: number
  src_flows: number
  dst_flows: number
}

export interface CountriesPage extends Omit<Paged<CountryRow>, 'remainder'> {
  remainder: SideRemainder | null
  available: boolean
  geoip: GeoStatus
  reason?: string
  prefixes?: { resolved: number; total: number }
}

export interface AsnRow extends SideRow {
  asn: number | null
  as_name: string | null
  kind: PlaceKind
  /** Where the AS number came from: the exporter's flow fields, GeoIP, or both. */
  source: 'flow' | 'geoip' | 'flow+geoip' | null
  hint?: string
}

export interface AsnsPage extends Omit<Paged<AsnRow>, 'remainder'> {
  remainder: SideRemainder | null
  basis_bytes: { flow: number; geoip: number; none: number }
  geoip: GeoStatus
  available: boolean
  prefixes?: { resolved: number; total: number }
}

export interface NextHopRow extends HostEnrichment {
  next_hop: string
  ip: string
  bytes: number
  packets: number
  flows: number
  share_pct: number | null
  exporters: { ip: string }[]
  out_interfaces: InterfaceRef[]
}

export interface NextHopsPage extends Paged<NextHopRow> {
  without_next_hop: { connected: Totals; not_exported: Totals; legacy: Totals }
  next_hop_coverage_pct: number | null
}

export interface IpGroupRow {
  group: string
  name: string
  description: string | null
  color: string | null
  cidrs: string[]
  src_bytes: number
  dst_bytes: number
  internal_bytes: number
  bytes: number
  packets: number
  flows: number
  share_pct: number | null
  selected: boolean
}

export interface IpGroupsPage extends Paged<IpGroupRow> {
  ungrouped?: { bytes: number; flows: number; share_pct: number | null }
  configured: boolean
}
