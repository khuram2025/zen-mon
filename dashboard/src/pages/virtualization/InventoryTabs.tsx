import { useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { Input } from '@/components/ui/Input'
import { Card } from '@/components/ui/Card'
import {
  attr,
  attrList,
  clusterOf,
  fmtBytes,
  fmtDuration,
  fmtHz,
  fmtLatency,
  fmtNumber,
  fmtRate,
  isOff,
  isOn,
  memoryFigure,
  plural,
  type Index,
  type Resource,
} from './model'
import { seriesVar } from '@/lib/chartTheme'
import {
  DataTable,
  HISTORY_RANGES,
  HealthBadge,
  HistoryChart,
  HistoryState,
  Legend,
  Meter,
  PowerBadge,
  ProviderLink,
  RangeSwitch,
  selectStyle,
  useHistory,
  type Column,
  type HistoryRange,
} from './ui'

function ClusterTrend({ connectorId, clusterId }: { connectorId: string; clusterId: string }) {
  const [range, setRange] = useState<HistoryRange>('24h')
  const history = useHistory(connectorId, clusterId, range)
  return (
    <div className="border-b border-border p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <Legend items={[{ label: 'CPU', color: seriesVar(0) }, { label: 'Memory', color: seriesVar(1) }, { label: 'Storage', color: seriesVar(2) }]} />
        <RangeSwitch value={range} onChange={setRange} />
      </div>
      <HistoryState query={history}>
        <HistoryChart
          data={history.data ?? []}
          hours={HISTORY_RANGES.find((r) => r.key === range)!.hours}
          height={180}
          series={[{ metric: 'cpu_usage_percent', label: 'CPU' }, { metric: 'memory_usage_percent', label: 'Memory' }, { metric: 'storage_usage_percent', label: 'Storage' }]}
        />
      </HistoryState>
    </div>
  )
}

type TabProps = { rows: Resource[]; index: Index; stale: boolean; onOpen: (id: string) => void; provider?: string; connectorId?: string }

function useSearch(rows: Resource[], extra: (r: Resource) => string = () => '') {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return needle ? rows.filter((r) => `${r.name} ${r.external_id} ${extra(r)}`.toLowerCase().includes(needle)) : rows
  }, [rows, q, extra])
  return { q, setQ, filtered }
}

function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div className="relative w-full max-w-sm">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden="true" />
      <Input className="pl-8" aria-label={placeholder} placeholder={placeholder} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  )
}

function AlertCount({ id, index }: { id: string; index: Index }) {
  const list = index.alertsByResource.get(id) ?? []
  if (!list.length) return <span className="text-muted">—</span>
  const critical = list.some((a) => a.severity === 'critical')
  return (
    <span className={`inline-flex min-w-[1.5rem] justify-center rounded-full px-1.5 text-xs font-semibold ${critical ? 'bg-danger/15 text-danger-fg' : 'bg-warning/15 text-warning-fg'}`}>
      {list.length}
    </span>
  )
}

const nameCell = (r: Resource, sub?: React.ReactNode) => (
  <div className="min-w-[10rem]">
    <div className="font-medium text-primary">{r.name}</div>
    {sub && <div className="truncate text-xs text-muted">{sub}</div>}
  </div>
)

/* ── Virtual machines ───────────────────────────────────────────────────── */

export function VmsTab({ rows, index, stale, onOpen }: TabProps) {
  const [power, setPower] = useState('all')
  const [host, setHost] = useState('all')
  const ips = (r: Resource) => `${attrList(r, 'ip_addresses').join(' ')} ${attr(r, 'guest_os') ?? ''}`
  const { q, setQ, filtered } = useSearch(rows, ips)
  const hosts = useMemo(
    () => [...new Set(rows.map((r) => r.parent_id).filter((p): p is string => !!p))]
      .map((id) => index.byId.get(id))
      .filter((r): r is Resource => !!r)
      .sort((a, b) => a.name.localeCompare(b.name)),
    [rows, index],
  )
  const visible = filtered.filter(
    (r) =>
      (power === 'all' || (power === 'on' ? isOn(r.power_state) : power === 'off' ? isOff(r.power_state) : !isOn(r.power_state) && !isOff(r.power_state))) &&
      (host === 'all' || r.parent_id === host),
  )
  const reservedCount = rows.filter((r) => memoryFigure(r).reserved && isOn(r.power_state)).length
  const columns: Column<Resource>[] = [
    {
      key: 'name',
      label: 'Virtual machine',
      sort: (r) => r.name.toLowerCase(),
      render: (r) => nameCell(r, [attrList(r, 'ip_addresses').slice(0, 2).join(', '), attr(r, 'guest_os')].filter(Boolean).join(' · ') || undefined),
    },
    { key: 'power', label: 'Power', sort: (r) => (isOn(r.power_state) ? 0 : isOff(r.power_state) ? 2 : 1), render: (r) => <PowerBadge power={r.power_state} /> },
    {
      key: 'host',
      label: 'Host',
      sort: (r) => (r.parent_id ? index.byId.get(r.parent_id)?.name : undefined),
      render: (r) => {
        const p = r.parent_id ? index.byId.get(r.parent_id) : undefined
        return p ? <span className={p.kind === 'cluster' ? 'text-muted' : ''}>{p.kind === 'cluster' ? `${p.name} (not placed)` : p.name}</span> : <span className="text-muted">—</span>
      },
    },
    { key: 'vcpu', label: 'vCPU', sort: (r) => r.metrics.cpu_cores, render: (r) => fmtNumber(r.metrics.cpu_cores), className: 'tabular-nums' },
    { key: 'cpu', label: 'CPU', sort: (r) => r.metrics.cpu_usage_percent, render: (r) => <Meter value={isOff(r.power_state) ? undefined : r.metrics.cpu_usage_percent} /> },
    {
      key: 'memory',
      label: 'Memory',
      sort: (r) => r.metrics.memory_usage_percent ?? (r.metrics.memory_capacity_bytes !== undefined ? -1 : undefined),
      render: (r) => {
        const m = memoryFigure(r)
        return m.percent !== undefined ? (
          <Meter value={m.percent} detail={m.detail} />
        ) : (
          <div className="min-w-[7rem]">
            <div>{m.detail || '—'}</div>
            {m.reserved && isOn(r.power_state) && <div className="text-xs text-muted" title="AHV reserves the full assignment; install guest tools for in-guest memory usage">usage needs guest tools</div>}
          </div>
        )
      },
    },
    {
      key: 'disk',
      label: 'Disk',
      sort: (r) => r.metrics.disk_provisioned_bytes ?? r.metrics.storage_capacity_bytes,
      render: (r) =>
        r.metrics.storage_usage_percent !== undefined ? (
          <Meter value={r.metrics.storage_usage_percent} detail={fmtBytes(r.metrics.storage_capacity_bytes)} />
        ) : (
          <span className="tabular-nums">{fmtBytes(r.metrics.disk_provisioned_bytes ?? r.metrics.storage_capacity_bytes)}</span>
        ),
    },
    { key: 'iops', label: 'IOPS', sort: (r) => r.metrics.iops, render: (r) => fmtNumber(r.metrics.iops), className: 'tabular-nums' },
    { key: 'latency', label: 'Latency', sort: (r) => r.metrics.io_latency_us, render: (r) => fmtLatency(r.metrics.io_latency_us), className: 'tabular-nums whitespace-nowrap' },
    { key: 'alerts', label: 'Alerts', sort: (r) => index.alertsByResource.get(r.id)?.length ?? 0, render: (r) => <AlertCount id={r.id} index={index} /> },
    { key: 'link', label: '', render: (r) => <ProviderLink url={r.url}>Open</ProviderLink> },
  ]
  if (stale) columns.splice(2, 0, { key: 'health', label: 'State', render: () => <HealthBadge health="" stale /> })
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 p-4">
        <SearchBox value={q} onChange={setQ} placeholder="Search name, IP, guest OS or ID" />
        <select aria-label="Power state" className={selectStyle} value={power} onChange={(e) => setPower(e.target.value)}>
          <option value="all">All power states</option>
          <option value="on">Powered on</option>
          <option value="off">Powered off</option>
          <option value="other">Other</option>
        </select>
        {hosts.length > 1 && (
          <select aria-label="Host" className={selectStyle} value={host} onChange={(e) => setHost(e.target.value)}>
            <option value="all">All hosts</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </select>
        )}
        <span className="ml-auto text-sm text-muted">{plural(visible.length, 'VM')}</span>
      </div>
      {reservedCount > 0 && (
        <p className="mx-4 mb-3 rounded-md border border-info/30 bg-info/5 px-3 py-2 text-xs text-text2">
          {plural(reservedCount, 'running VM')} report{reservedCount === 1 ? 's' : ''} only hypervisor-reserved memory (AHV allocates the full assignment).
          In-guest memory utilization appears once Nutanix Guest Tools are installed and enabled.
        </p>
      )}
      <DataTable rows={visible} columns={columns} onRowClick={(r) => onOpen(r.id)} empty="No virtual machines match." initialSort={{ key: 'cpu', order: 'desc' }} />
    </Card>
  )
}

/* ── Hosts ──────────────────────────────────────────────────────────────── */

export function HostsTab({ rows, index, stale, onOpen }: TabProps) {
  const { q, setQ, filtered } = useSearch(rows, (r) => `${attr(r, 'hypervisor_ip') ?? ''} ${attr(r, 'cvm_ip') ?? ''} ${attr(r, 'serial') ?? ''}`)
  const vmsOf = (r: Resource) => (index.children.get(r.id) ?? []).filter((c) => c.kind === 'vm')
  const columns: Column<Resource>[] = [
    { key: 'name', label: 'Host', sort: (r) => r.name.toLowerCase(), render: (r) => nameCell(r, [attr(r, 'hypervisor_ip'), attr(r, 'hypervisor')].filter(Boolean).join(' · ') || undefined) },
    { key: 'cluster', label: 'Cluster', sort: (r) => clusterOf(r, index)?.name, render: (r) => clusterOf(r, index)?.name ?? <span className="text-muted">—</span> },
    {
      key: 'health',
      label: 'Health',
      sort: (r) => r.metrics.health_score,
      render: (r) => (
        <div>
          <HealthBadge health={r.health} stale={stale} />
          {r.metrics.health_score !== undefined && <div className="text-xs text-muted">score {fmtNumber(r.metrics.health_score)}</div>}
        </div>
      ),
    },
    { key: 'cpu', label: 'CPU', sort: (r) => r.metrics.cpu_usage_percent, render: (r) => <Meter value={r.metrics.cpu_usage_percent} detail={[r.metrics.cpu_cores !== undefined && `${fmtNumber(r.metrics.cpu_cores)} cores`, r.metrics.cpu_capacity_hz !== undefined && fmtHz(r.metrics.cpu_capacity_hz)].filter(Boolean).join(' · ')} /> },
    { key: 'memory', label: 'Memory', sort: (r) => r.metrics.memory_usage_percent, render: (r) => <Meter value={r.metrics.memory_usage_percent} detail={memoryFigure(r).detail} /> },
    { key: 'storage', label: 'Storage', sort: (r) => r.metrics.storage_usage_percent, render: (r) => <Meter value={r.metrics.storage_usage_percent} detail={r.metrics.storage_capacity_bytes !== undefined ? fmtBytes(r.metrics.storage_capacity_bytes) : undefined} /> },
    {
      key: 'vms',
      label: 'VMs',
      sort: (r) => vmsOf(r).length,
      render: (r) => {
        const list = vmsOf(r)
        return <span className="tabular-nums">{list.filter((v) => isOn(v.power_state)).length}<span className="text-muted"> / {list.length}</span></span>
      },
    },
    { key: 'iops', label: 'IOPS', sort: (r) => r.metrics.iops, render: (r) => fmtNumber(r.metrics.iops), className: 'tabular-nums' },
    { key: 'latency', label: 'Latency', sort: (r) => r.metrics.io_latency_us, render: (r) => fmtLatency(r.metrics.io_latency_us), className: 'tabular-nums whitespace-nowrap' },
    { key: 'uptime', label: 'Uptime', sort: (r) => r.metrics.uptime_seconds, render: (r) => fmtDuration(r.metrics.uptime_seconds), className: 'whitespace-nowrap' },
    { key: 'alerts', label: 'Alerts', sort: (r) => index.alertsByResource.get(r.id)?.length ?? 0, render: (r) => <AlertCount id={r.id} index={index} /> },
  ]
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 p-4">
        <SearchBox value={q} onChange={setQ} placeholder="Search host, IP or serial" />
        <span className="ml-auto text-sm text-muted">{plural(filtered.length, 'host')}</span>
      </div>
      <DataTable rows={filtered} columns={columns} onRowClick={(r) => onOpen(r.id)} empty="No hosts match." initialSort={{ key: 'name', order: 'asc' }} />
    </Card>
  )
}

/* ── Clusters ───────────────────────────────────────────────────────────── */

export function ClustersTab({ rows, index, stale, onOpen, connectorId }: TabProps) {
  if (!rows.length) return <Card className="p-8 text-center text-sm text-muted">No clusters reported by this connector.</Card>
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {rows.map((c) => {
        const hosts = (index.children.get(c.id) ?? []).filter((r) => r.kind === 'host')
        const vms = [...hosts.flatMap((h) => index.children.get(h.id) ?? []), ...(index.children.get(c.id) ?? [])].filter((r) => r.kind === 'vm')
        const storage = (index.children.get(c.id) ?? []).filter((r) => r.kind === 'storage')
        const m = c.metrics
        const facts: [string, React.ReactNode][] = [
          ['Software', attr(c, 'version') ?? '—'],
          ['Nodes', attr(c, 'nodes') ?? hosts.length],
          ['Virtual IP', attr(c, 'virtual_ip') ?? '—'],
          ['Hypervisors', attr(c, 'hypervisors') ?? '—'],
          ['Redundancy factor', attr(c, 'redundancy_factor') ?? '—'],
          ['VMs', `${vms.filter((v) => isOn(v.power_state)).length} running / ${vms.length}`],
          ['Storage containers', storage.length],
          ['Active alerts', index.alertsByResource.get(c.id)?.length ?? 0],
        ]
        return (
          <Card key={c.id} className="overflow-hidden">
            <button type="button" onClick={() => onOpen(c.id)} className="flex w-full items-start justify-between gap-3 border-b border-border p-4 text-left hover:bg-surface2/50">
              <div>
                <div className="text-lg font-semibold text-primary">{c.name}</div>
                <div className="text-xs text-muted">{plural(hosts.length, 'host')} · {plural(vms.length, 'VM')}</div>
              </div>
              <div className="text-right">
                <HealthBadge health={c.health} stale={stale} />
                {m.health_score !== undefined && <div className="mt-1 text-xs text-muted">health score {fmtNumber(m.health_score)}</div>}
              </div>
            </button>
            <div className="grid gap-4 p-4 sm:grid-cols-3">
              <div><div className="mb-1 text-xs text-muted">CPU</div><Meter value={m.cpu_usage_percent} detail={m.cpu_capacity_hz !== undefined ? fmtHz(m.cpu_capacity_hz) : undefined} /></div>
              <div><div className="mb-1 text-xs text-muted">Memory</div><Meter value={m.memory_usage_percent} detail={memoryFigure(c).detail} /></div>
              <div><div className="mb-1 text-xs text-muted">Storage</div><Meter value={m.storage_usage_percent} detail={m.storage_capacity_bytes !== undefined ? `${fmtBytes(m.storage_used_bytes)} of ${fmtBytes(m.storage_capacity_bytes)}` : undefined} /></div>
            </div>
            <div className="grid grid-cols-3 gap-px border-y border-border bg-border text-center">
              {[
                ['IOPS', fmtNumber(m.iops)],
                ['Latency', fmtLatency(m.io_latency_us)],
                ['Throughput', fmtRate(m.io_bandwidth_bytes_per_second)],
              ].map(([k, v]) => (
                <div key={k} className="bg-surface p-3">
                  <div className="text-2xs uppercase tracking-wider text-muted">{k}</div>
                  <div className="mt-1 font-semibold tabular-nums">{v}</div>
                </div>
              ))}
            </div>
            {connectorId && <ClusterTrend connectorId={connectorId} clusterId={c.id} />}
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 p-4 text-sm">
              {facts.map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2">
                  <dt className="text-muted">{k}</dt>
                  <dd className="truncate text-right font-medium">{v}</dd>
                </div>
              ))}
            </dl>
          </Card>
        )
      })}
    </div>
  )
}

/* ── Storage ────────────────────────────────────────────────────────────── */

export function StorageTab({ rows, index, onOpen, provider }: TabProps) {
  const { q, setQ, filtered } = useSearch(rows)
  const columns: Column<Resource>[] = [
    { key: 'name', label: 'Container / datastore', sort: (r) => r.name.toLowerCase(), render: (r) => nameCell(r, clusterOf(r, index)?.name) },
    { key: 'usage', label: 'Usage', sort: (r) => r.metrics.storage_usage_percent, render: (r) => <Meter value={r.metrics.storage_usage_percent} className="min-w-[10rem]" /> },
    { key: 'used', label: 'Used', sort: (r) => r.metrics.storage_used_bytes, render: (r) => fmtBytes(r.metrics.storage_used_bytes), className: 'tabular-nums whitespace-nowrap' },
    {
      key: 'free',
      label: 'Free',
      sort: (r) => (r.metrics.storage_capacity_bytes !== undefined ? r.metrics.storage_capacity_bytes - (r.metrics.storage_used_bytes ?? 0) : undefined),
      render: (r) => (r.metrics.storage_capacity_bytes !== undefined ? fmtBytes(r.metrics.storage_capacity_bytes - (r.metrics.storage_used_bytes ?? 0)) : '—'),
      className: 'tabular-nums whitespace-nowrap',
    },
    { key: 'capacity', label: 'Capacity', sort: (r) => r.metrics.storage_capacity_bytes, render: (r) => fmtBytes(r.metrics.storage_capacity_bytes), className: 'tabular-nums whitespace-nowrap' },
    { key: 'iops', label: 'IOPS', sort: (r) => r.metrics.iops, render: (r) => fmtNumber(r.metrics.iops), className: 'tabular-nums' },
    { key: 'latency', label: 'Latency', sort: (r) => r.metrics.io_latency_us, render: (r) => fmtLatency(r.metrics.io_latency_us), className: 'tabular-nums whitespace-nowrap' },
    { key: 'throughput', label: 'Throughput', sort: (r) => r.metrics.io_bandwidth_bytes_per_second, render: (r) => fmtRate(r.metrics.io_bandwidth_bytes_per_second), className: 'tabular-nums whitespace-nowrap' },
    {
      key: 'features',
      label: 'Data services',
      render: (r) => [attr(r, 'replication_factor') && `RF${attr(r, 'replication_factor')}`, attr(r, 'compression') && 'Compression', attr(r, 'deduplication') && 'Dedup', attr(r, 'erasure_coding') && 'EC', attr(r, 'type')].filter(Boolean).join(' · ') || <span className="text-muted">—</span>,
      className: 'text-xs whitespace-nowrap',
    },
  ]
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 p-4">
        <SearchBox value={q} onChange={setQ} placeholder="Search storage" />
        <span className="ml-auto text-sm text-muted">{plural(filtered.length, 'container')}</span>
      </div>
      {provider?.startsWith('nutanix') && (
        <p className="mx-4 mb-3 text-xs text-muted">
          Nutanix containers share the cluster storage pool, so each container's capacity is the pool space available to it.
        </p>
      )}
      <DataTable rows={filtered} columns={columns} onRowClick={(r) => onOpen(r.id)} empty="No storage matches." initialSort={{ key: 'used', order: 'desc' }} />
    </Card>
  )
}
