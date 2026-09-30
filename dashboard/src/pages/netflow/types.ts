type Overview = {
  bytes: number
  packets: number
  flows: number
  exporters: number
  src_hosts: number
  dst_hosts: number
  last_seen: string | null
  current_bps: number
  top_protocol: { protocol: number; name: string; bytes: number } | null
}

type SeriesPoint = { ts: number; bps: number; bytes: number; packets: number; flows: number }
type Talker = { ip: string; bytes: number; packets: number; flows: number }
type Endpoint = { ip: string; bytes: number; packets: number; flows: number; src_bytes: number; dst_bytes: number; src_flows: number; dst_flows: number }
type NetflowInterfaceRef = {
  exporter_ip: string
  ifindex: number
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null
  device_hostname?: string | null
  display_name?: string | null
}
type Conversation = {
  src: string
  dst: string
  protocol: number
  protocol_name: string
  dst_port: number
  service: string
  application?: string
  port_class?: string
  src_ports?: number[]
  bytes: number
  packets: number
  flows: number
  exporters?: { ip: string; hostname?: string | null }[]
  input_snmp?: number[]
  output_snmp?: number[]
  input_interfaces?: NetflowInterfaceRef[]
  output_interfaces?: NetflowInterfaceRef[]
  first_seen?: string | null
  last_seen?: string | null
  received_at?: string | null
  avg_duration_ms?: number
  tcp_flags?: number
  avg_bytes?: number
  avg_packets?: number
}
type Protocol = { protocol: number; name: string; bytes: number; packets: number; flows: number }
type Application = { name: string; bytes: number; packets: number; flows: number }
type Exporter = { exporter_ip: string; bytes: number; packets: number; flows: number; last_seen: string }
type DeviceStatus = { latency_ms: number; packet_loss_pct: number; uptime_pct: number; flows: number; packets: number; exporters: number; last_seen: string | null }
type Heatmap = { max_bytes: number; cells: { dow: number; hour: number; bytes: number; flows: number }[] }
type ExporterDevice = {
  exporter_ip: string
  flows: number
  last_seen: string | null
  device_id: string | null
  device_hostname: string | null
  /** 'ip-match' = exporter IP is a device's management IP, 'mapped' = linked by an
   *  operator, null = unresolved, so its interfaces can only show raw ifIndex. */
  source: 'ip-match' | 'mapped' | null
}
type Interface = {
  exporter_ip: string
  ifindex: number
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null
  device_hostname?: string | null
  display_name?: string | null
  in_bytes: number
  out_bytes: number
  bytes: number
  in_packets: number
  out_packets: number
  packets: number
  in_flows: number
  out_flows: number
  flows: number
}

type Country = { country: string | null; country_name: string | null; bytes: number; packets: number; flows: number }
type DscpClass = { dscp: number; label: string; bytes: number; packets: number; flows: number }
type TcpFlagSummary = { total_tcp: number; syn_only: number; ack_only: number; rst: number; fin: number; psh: number; urg: number; no_flags: number }
type NetClass = { name: string; bytes: number; packets: number; flows: number }

type Alert = {
  id: string
  title: string
  body: string
  severity: 'critical' | 'warning' | 'info' | 'success'
  icon: React.ComponentType<{ className?: string }>
  ts?: string
}

type SortKey = 'src' | 'dst' | 'service' | 'protocol_name' | 'bytes' | 'packets'

export type { Overview, SeriesPoint, Talker, Endpoint, NetflowInterfaceRef, Conversation, Protocol, Application, Exporter, DeviceStatus, Heatmap, ExporterDevice, Interface, Country, DscpClass, TcpFlagSummary, NetClass, Alert, SortKey }
