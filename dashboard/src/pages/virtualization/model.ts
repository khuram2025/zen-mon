/** Virtualization connector data model, formatting and derived figures shared by the tabs. */

export const providers = {
  nutanix_pc: 'Nutanix Prism Central',
  nutanix_pe: 'Nutanix Prism Element',
  vmware: 'VMware vCenter / ESXi',
  hyperv: 'Microsoft Hyper-V',
}
export type Provider = keyof typeof providers
export type Kind = 'cluster' | 'host' | 'vm' | 'storage'

export type Resource = {
  id: string
  external_id: string
  kind: Kind | string
  name: string
  parent_id?: string | null
  health: string
  power_state?: string | null
  metrics: Record<string, number>
  attributes?: Record<string, string | string[]>
  url?: string | null
}
export type Alert = {
  id: string
  title: string
  severity: string
  resolved: boolean
  acknowledged: boolean
  message?: string | null
  created_at?: string | null
  updated_at?: string | null
  resolved_at?: string | null
  source_name?: string | null
  source_type?: string | null
  impact?: string | null
  resource_id?: string | null
  resource_url?: string | null
  url?: string | null
}
export type Summary = {
  version?: string
  counts?: Record<string, number>
  warnings?: string[]
  capabilities?: Record<string, boolean | number>
  alert_count?: number
}
export type Connector = {
  id: string
  name: string
  provider: Provider
  endpoint: string
  tags: string[]
  enabled: boolean
  interval_seconds: number
  status: string
  stale: boolean
  last_error?: string | null
  last_attempt_at?: string | null
  last_success_at?: string | null
  summary: Summary
  resources?: Resource[]
  alerts?: Alert[]
}
export type HistoryPoint = { time: string; metric: string; value: number }

/* ── Formatting ─────────────────────────────────────────────────────────── */

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']

export function fmtBytes(v?: number, digits = 1): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return '—'
  const i = v >= 1 ? Math.min(BYTE_UNITS.length - 1, Math.floor(Math.log(v) / Math.log(1024))) : 0
  return `${(v / 1024 ** i).toFixed(i === 0 ? 0 : digits)} ${BYTE_UNITS[i]}`
}

export function fmtRate(v?: number): string {
  return v === undefined ? '—' : `${fmtBytes(v)}/s`
}

export function fmtPct(v?: number, digits = 1): string {
  return v === undefined || !Number.isFinite(v) ? '—' : `${v.toFixed(digits)}%`
}

export function fmtLatency(us?: number): string {
  if (us === undefined || !Number.isFinite(us)) return '—'
  if (us >= 1000) return `${(us / 1000).toFixed(us >= 100000 ? 0 : 2)} ms`
  return `${Math.round(us)} µs`
}

export function fmtNumber(v?: number, digits = 0): string {
  return v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { maximumFractionDigits: digits })
}

export function fmtHz(v?: number): string {
  if (v === undefined) return '—'
  return v >= 1e9 ? `${(v / 1e9).toFixed(1)} GHz` : `${(v / 1e6).toFixed(0)} MHz`
}

export function fmtDuration(seconds?: number): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

export const KIND_LABEL: Record<string, { one: string; many: string }> = {
  cluster: { one: 'cluster', many: 'clusters' },
  host: { one: 'host', many: 'hosts' },
  vm: { one: 'virtual machine', many: 'virtual machines' },
  storage: { one: 'storage container', many: 'storage containers' },
}

/** Metric catalogue: label, formatter, unit for chart axes, and whether higher is worse (meters). */
type MetricSpec = { label: string; format: (v: number) => string; axis?: (v: number) => string; percent?: boolean }
const pct: MetricSpec['format'] = (v) => fmtPct(v)
export const METRICS: Record<string, MetricSpec> = {
  cpu_usage_percent: { label: 'CPU utilization', format: pct, percent: true },
  memory_usage_percent: { label: 'Memory utilization', format: pct, percent: true },
  hypervisor_memory_usage_percent: { label: 'Hypervisor memory (reserved)', format: pct, percent: true },
  storage_usage_percent: { label: 'Storage utilization', format: pct, percent: true },
  memory_demand_percent: { label: 'Memory demand / assigned', format: pct, percent: true },
  cpu_ready_percent: { label: 'CPU ready', format: (v) => fmtPct(v, 2), percent: true },
  health_score: { label: 'Health score', format: (v) => fmtNumber(v) },
  iops: { label: 'IOPS', format: (v) => fmtNumber(v) },
  read_iops: { label: 'Read IOPS', format: (v) => fmtNumber(v) },
  write_iops: { label: 'Write IOPS', format: (v) => fmtNumber(v) },
  io_latency_us: { label: 'I/O latency', format: fmtLatency, axis: (v) => fmtLatency(v) },
  read_latency_us: { label: 'Read latency', format: fmtLatency, axis: (v) => fmtLatency(v) },
  write_latency_us: { label: 'Write latency', format: fmtLatency, axis: (v) => fmtLatency(v) },
  io_bandwidth_bytes_per_second: { label: 'I/O throughput', format: fmtRate, axis: (v) => fmtRate(v) },
  network_bytes_per_second: { label: 'Network throughput', format: fmtRate, axis: (v) => fmtRate(v) },
  network_received_bytes: { label: 'Network received (per sample)', format: (v) => fmtBytes(v), axis: (v) => fmtBytes(v, 0) },
  network_transmitted_bytes: { label: 'Network sent (per sample)', format: (v) => fmtBytes(v), axis: (v) => fmtBytes(v, 0) },
  cpu_cores: { label: 'CPU cores', format: (v) => fmtNumber(v) },
  cpu_capacity_hz: { label: 'CPU capacity', format: fmtHz },
  cpu_used_hz: { label: 'CPU used', format: fmtHz },
  cpu_ready_ms: { label: 'CPU ready (ms / sample)', format: (v) => fmtNumber(v, 1) },
  memory_capacity_bytes: { label: 'Memory capacity', format: (v) => fmtBytes(v) },
  memory_used_bytes: { label: 'Memory used', format: (v) => fmtBytes(v) },
  memory_assigned_bytes: { label: 'Assigned memory', format: (v) => fmtBytes(v) },
  memory_demand_bytes: { label: 'Memory demand', format: (v) => fmtBytes(v) },
  memory_balloon_bytes: { label: 'Ballooned memory', format: (v) => fmtBytes(v) },
  storage_capacity_bytes: { label: 'Storage capacity', format: (v) => fmtBytes(v) },
  storage_used_bytes: { label: 'Storage used', format: (v) => fmtBytes(v) },
  disk_provisioned_bytes: { label: 'Provisioned disk', format: (v) => fmtBytes(v) },
  uptime_seconds: { label: 'Uptime', format: fmtDuration },
}
export const metricLabel = (k: string) => METRICS[k]?.label ?? k.replaceAll('_', ' ')
export const metricValue = (k: string, v: number) => (METRICS[k]?.format ?? ((x: number) => fmtNumber(x, 2)))(v)

/** Charts shown first in a resource's performance tab, in this order, when samples exist. */
export const CHART_ORDER = [
  'cpu_usage_percent',
  'memory_usage_percent',
  'storage_usage_percent',
  'iops',
  'io_latency_us',
  'io_bandwidth_bytes_per_second',
  'network_received_bytes',
  'network_transmitted_bytes',
  'network_bytes_per_second',
  'read_iops',
  'write_iops',
  'read_latency_us',
  'write_latency_us',
  'cpu_ready_percent',
  'memory_demand_percent',
  'hypervisor_memory_usage_percent',
  'health_score',
]

export const ATTRIBUTE_LABEL: Record<string, string> = {
  version: 'Software version',
  hypervisors: 'Hypervisors',
  nodes: 'Nodes',
  virtual_ip: 'Virtual IP',
  data_services_ip: 'Data services IP',
  redundancy_factor: 'Redundancy factor',
  operation_mode: 'Operation mode',
  timezone: 'Time zone',
  hypervisor: 'Hypervisor',
  hypervisor_ip: 'Hypervisor IP',
  cvm_ip: 'CVM IP',
  ipmi_ip: 'IPMI IP',
  model: 'Model',
  vendor: 'Vendor',
  serial: 'Serial',
  cpu_model: 'CPU model',
  cpu_sockets: 'CPU sockets',
  maintenance: 'Maintenance',
  node_status: 'Node status',
  connection: 'Connection',
  ip_addresses: 'IP addresses',
  guest_os: 'Guest OS',
  guest_tools: 'Guest tools',
  hostname: 'Guest hostname',
  description: 'Description',
  created: 'Created',
  machine_type: 'Machine type',
  boot: 'Boot',
  vcpu_topology: 'vCPU topology',
  replication_factor: 'Replication factor',
  compression: 'Compression',
  deduplication: 'Deduplication',
  erasure_coding: 'Erasure coding',
  encrypted: 'Encrypted',
  type: 'Type',
}

/* ── State helpers ──────────────────────────────────────────────────────── */

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'default' | 'outline'

export function healthTone(h: string): Tone {
  if (h === 'healthy') return 'success'
  if (h === 'warning') return 'warning'
  if (h === 'critical') return 'danger'
  return 'outline'
}

export function isOn(power?: string | null): boolean {
  const p = (power || '').toLowerCase()
  return p === 'on' || p === 'poweredon' || p === 'running'
}
export function isOff(power?: string | null): boolean {
  const p = (power || '').toLowerCase()
  return p === 'off' || p === 'poweredoff' || p === 'stopped'
}
export function powerLabel(power?: string | null): string {
  if (!power) return '—'
  if (isOn(power)) return 'On'
  if (isOff(power)) return 'Off'
  const p = power.toLowerCase()
  if (p === 'suspended' || p === 'paused' || p === 'saved') return power.charAt(0).toUpperCase() + p.slice(1)
  return power
}

export const SEVERITY_RANK: Record<string, number> = { critical: 0, error: 0, warning: 1, info: 2 }
export function severityTone(s: string): Tone {
  if (s === 'critical' || s === 'error') return 'danger'
  if (s === 'warning') return 'warning'
  if (s === 'info') return 'info'
  return 'default'
}

export function attr(r: Resource, key: string): string | undefined {
  const v = r.attributes?.[key]
  return Array.isArray(v) ? v.join(', ') : v
}
export function attrList(r: Resource, key: string): string[] {
  const v = r.attributes?.[key]
  return Array.isArray(v) ? v : v ? [v] : []
}

/** Memory figure for a row: utilization when known, otherwise assigned capacity only. */
export function memoryFigure(r: Resource): { percent?: number; detail: string; reserved: boolean } {
  const m = r.metrics
  const cap = m.memory_capacity_bytes
  const reserved = r.kind === 'vm' && m.memory_usage_percent === undefined && m.hypervisor_memory_usage_percent !== undefined
  if (m.memory_usage_percent !== undefined) {
    const used = m.memory_used_bytes ?? (cap !== undefined ? (cap * m.memory_usage_percent) / 100 : undefined)
    return {
      percent: m.memory_usage_percent,
      detail: cap !== undefined ? `${fmtBytes(used)} of ${fmtBytes(cap)}` : '',
      reserved,
    }
  }
  return { detail: cap !== undefined ? `${fmtBytes(cap)} assigned` : '', reserved }
}

export type Index = {
  byId: Map<string, Resource>
  children: Map<string, Resource[]>
  alertsByResource: Map<string, Alert[]>
}

export function buildIndex(resources: Resource[], alerts: Alert[]): Index {
  const byId = new Map(resources.map((r) => [r.id, r]))
  const children = new Map<string, Resource[]>()
  for (const r of resources) {
    if (!r.parent_id) continue
    const list = children.get(r.parent_id) ?? []
    list.push(r)
    children.set(r.parent_id, list)
  }
  const alertsByResource = new Map<string, Alert[]>()
  for (const a of alerts) {
    if (!a.resource_id || a.resolved) continue
    const list = alertsByResource.get(a.resource_id) ?? []
    list.push(a)
    alertsByResource.set(a.resource_id, list)
  }
  return { byId, children, alertsByResource }
}

/** The cluster a resource belongs to (itself for clusters), walking host → cluster. */
export function clusterOf(r: Resource, index: Index): Resource | undefined {
  let cur: Resource | undefined = r
  for (let i = 0; cur && i < 4; i++) {
    if (cur.kind === 'cluster') return cur
    cur = cur.parent_id ? index.byId.get(cur.parent_id) : undefined
  }
  return undefined
}

export function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort(
    (a, b) =>
      Number(a.resolved) - Number(b.resolved) ||
      (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3) ||
      String(b.created_at || '').localeCompare(String(a.created_at || '')),
  )
}

/** Aggregate cluster figures (weighted by capacity) for the overview when several clusters exist. */
export function aggregate(clusters: Resource[], key: 'cpu' | 'memory' | 'storage'): number | undefined {
  const capKey = key === 'cpu' ? 'cpu_capacity_hz' : key === 'memory' ? 'memory_capacity_bytes' : 'storage_capacity_bytes'
  const pctKey = `${key}_usage_percent`
  const rows = clusters.filter((c) => c.metrics[pctKey] !== undefined)
  if (!rows.length) return undefined
  if (rows.length === 1) return rows[0].metrics[pctKey]
  const weighted = rows.filter((c) => c.metrics[capKey])
  if (weighted.length === rows.length) {
    const total = weighted.reduce((s, c) => s + c.metrics[capKey], 0)
    return weighted.reduce((s, c) => s + (c.metrics[capKey] * c.metrics[pctKey]) / 100, 0) / total * 100
  }
  return rows.reduce((s, c) => s + c.metrics[pctKey], 0) / rows.length
}
