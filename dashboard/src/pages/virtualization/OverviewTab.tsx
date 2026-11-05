import { useMemo, useState } from 'react'
import {
  AlertTriangle,
  Boxes,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  MemoryStick,
  Monitor,
  Server,
} from 'lucide-react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { chartFont, seriesVar } from '@/lib/chartTheme'
import { relativeTime } from '@/lib/utils'
import { chartTooltipStyle } from '@/pages/dashboards/shared'
import {
  aggregate,
  fmtBytes,
  fmtLatency,
  fmtNumber,
  fmtPct,
  fmtRate,
  isOff,
  isOn,
  plural,
  sortAlerts,
  type Alert,
  type Connector,
  type Index,
  type Resource,
} from './model'
import {
  HISTORY_RANGES,
  HistoryChart,
  HistoryState,
  Legend,
  Meter,
  Panel,
  RangeSwitch,
  SeverityBadge,
  Tile,
  useHistory,
  type HistoryRange,
} from './ui'

export type TabKey = 'overview' | 'vms' | 'hosts' | 'clusters' | 'storage' | 'alerts'

export function OverviewTab({ connector, resources, alerts, index, onOpen, onTab }: {
  connector: Connector
  resources: Resource[]
  alerts: Alert[]
  index: Index
  onOpen: (id: string) => void
  onTab: (tab: TabKey) => void
}) {
  const clusters = resources.filter((r) => r.kind === 'cluster')
  const hosts = resources.filter((r) => r.kind === 'host')
  const vms = resources.filter((r) => r.kind === 'vm')
  const storage = resources.filter((r) => r.kind === 'storage')
  const active = alerts.filter((a) => !a.resolved)
  const critical = active.filter((a) => a.severity === 'critical' || a.severity === 'error').length
  const warning = active.filter((a) => a.severity === 'warning').length
  const vmsOn = vms.filter((v) => isOn(v.power_state)).length
  const vmsOff = vms.filter((v) => isOff(v.power_state)).length
  const unhealthyHosts = hosts.filter((h) => h.health === 'critical' || h.health === 'warning').length
  const cpu = aggregate(clusters.length ? clusters : hosts, 'cpu')
  const memory = aggregate(clusters.length ? clusters : hosts, 'memory')
  const storagePct = aggregate(clusters.length ? clusters : storage, 'storage')
  const sum = (rows: Resource[], key: string) => {
    const vals = rows.map((r) => r.metrics[key]).filter((v): v is number => v !== undefined)
    return vals.length ? vals.reduce((a, b) => a + b, 0) : undefined
  }
  const base = clusters.length ? clusters : hosts
  const memCap = sum(base, 'memory_capacity_bytes')
  const storageCap = sum(clusters.length ? clusters : storage, 'storage_capacity_bytes')
  const storageUsed = sum(clusters.length ? clusters : storage, 'storage_used_bytes')
  const iops = sum(clusters.length ? clusters : hosts, 'iops')
  const latency = clusters.length === 1 ? clusters[0].metrics.io_latency_us : undefined
  const throughput = sum(clusters.length ? clusters : hosts, 'io_bandwidth_bytes_per_second')
  const vcpus = sum(vms.filter((v) => isOn(v.power_state)), 'cpu_cores')
  const cores = sum(hosts, 'cpu_cores')
  const vmMemory = sum(vms.filter((v) => isOn(v.power_state)), 'memory_capacity_bytes')
  const hostMemory = sum(hosts, 'memory_capacity_bytes')

  const [clusterId, setClusterId] = useState('')
  const chartCluster = clusters.find((c) => c.id === clusterId) ?? clusters[0] ?? hosts[0]
  const [range, setRange] = useState<HistoryRange>('24h')
  const hours = HISTORY_RANGES.find((r) => r.key === range)!.hours
  const history = useHistory(connector.id, chartCluster?.id, range)

  const topCpu = useMemo(
    () =>
      vms
        .filter((v) => isOn(v.power_state) && v.metrics.cpu_usage_percent !== undefined)
        .sort((a, b) => b.metrics.cpu_usage_percent - a.metrics.cpu_usage_percent)
        .slice(0, 8)
        .map((v) => ({ id: v.id, name: v.name, value: +v.metrics.cpu_usage_percent.toFixed(2) })),
    [vms],
  )
  const topIops = useMemo(
    () =>
      vms
        .filter((v) => (v.metrics.iops ?? 0) > 0)
        .sort((a, b) => (b.metrics.iops ?? 0) - (a.metrics.iops ?? 0))
        .slice(0, 8)
        .map((v) => ({ id: v.id, name: v.name, value: Math.round(v.metrics.iops) })),
    [vms],
  )
  const hostBars = hosts.map((h) => ({
    id: h.id,
    name: h.name,
    cpu: h.metrics.cpu_usage_percent !== undefined ? +h.metrics.cpu_usage_percent.toFixed(1) : null,
    memory: h.metrics.memory_usage_percent !== undefined ? +h.metrics.memory_usage_percent.toFixed(1) : null,
    vms: (index.children.get(h.id) ?? []).filter((c) => c.kind === 'vm' && isOn(c.power_state)).length,
  }))
  const power = [
    { name: 'Powered on', value: vmsOn, color: 'rgb(var(--success))' },
    { name: 'Powered off', value: vmsOff, color: 'rgb(var(--border-strong))' },
    { name: 'Other', value: vms.length - vmsOn - vmsOff, color: 'rgb(var(--warning))' },
  ].filter((p) => p.value > 0)
  const topAlerts = sortAlerts(active).slice(0, 6)

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Virtual machines"
          value={vms.length.toLocaleString()}
          sub={`${vmsOn} running · ${vmsOff} off`}
          icon={<Monitor className="h-4 w-4" />}
          onClick={() => onTab('vms')}
        />
        <Tile
          label="Hosts"
          value={hosts.length.toLocaleString()}
          sub={
            cores !== undefined
              ? `${fmtNumber(cores)} cores · ${fmtBytes(hostMemory)} RAM`
              : unhealthyHosts
                ? `${unhealthyHosts} need attention`
                : 'All reporting'
          }
          tone={unhealthyHosts ? 'warning' : 'success'}
          icon={<Server className="h-4 w-4" />}
          onClick={() => onTab('hosts')}
        />
        <Tile
          label="Clusters"
          value={clusters.length.toLocaleString()}
          sub={
            clusters.length
              ? clusters.map((c) => `${c.name}: ${c.metrics.health_score !== undefined ? `score ${fmtNumber(c.metrics.health_score)}` : c.health}`).join(' · ')
              : 'None reported'
          }
          tone={clusters.some((c) => c.health === 'critical') ? 'danger' : clusters.some((c) => c.health === 'warning') ? 'warning' : 'success'}
          icon={<Boxes className="h-4 w-4" />}
          onClick={() => onTab('clusters')}
        />
        <Tile
          label="Active alerts"
          value={active.length.toLocaleString()}
          sub={`${critical} critical · ${warning} warning`}
          tone={critical ? 'danger' : warning ? 'warning' : 'success'}
          icon={<AlertTriangle className="h-4 w-4" />}
          onClick={() => onTab('alerts')}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card4 label="CPU" icon={<Cpu className="h-4 w-4" />} value={cpu}
          detail={vcpus !== undefined && cores ? `${fmtNumber(vcpus)} vCPU on ${fmtNumber(cores)} cores (${(vcpus / cores).toFixed(1)}:1)` : undefined} />
        <Card4 label="Memory" icon={<MemoryStick className="h-4 w-4" />} value={memory}
          detail={memCap !== undefined ? `${fmtBytes(memory !== undefined ? (memCap * memory) / 100 : undefined)} of ${fmtBytes(memCap)}${vmMemory !== undefined && hostMemory ? ` · ${fmtPct((vmMemory / hostMemory) * 100, 0)} to VMs` : ''}` : undefined} />
        <Card4 label="Storage" icon={<HardDrive className="h-4 w-4" />} value={storagePct}
          detail={storageCap !== undefined ? `${fmtBytes(storageUsed)} of ${fmtBytes(storageCap)} · ${fmtBytes(storageCap - (storageUsed ?? 0))} free` : undefined} />
        <Tile
          label="Storage performance"
          value={iops !== undefined ? `${fmtNumber(iops)} IOPS` : '—'}
          sub={[latency !== undefined && `${fmtLatency(latency)} latency`, throughput !== undefined && fmtRate(throughput)].filter(Boolean).join(' · ') || 'No I/O counters'}
          icon={<Gauge className="h-4 w-4" />}
        />
      </div>

      {chartCluster && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Panel
            title={`${chartCluster.kind === 'cluster' ? 'Cluster' : 'Host'} utilization`}
            hint={chartCluster.name}
            right={
              <div className="flex items-center gap-2">
                {clusters.length > 1 && (
                  <select aria-label="Cluster" className="h-8 rounded-md border border-border bg-surface px-2 text-xs" value={chartCluster.id} onChange={(e) => setClusterId(e.target.value)}>
                    {clusters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                )}
                <RangeSwitch value={range} onChange={setRange} />
              </div>
            }
          >
            <HistoryState query={history}>
              <Legend items={[
                { label: 'CPU', color: seriesVar(0) },
                { label: 'Memory', color: seriesVar(1) },
                { label: 'Storage', color: seriesVar(2) },
              ]} />
              <HistoryChart data={history.data ?? []} hours={hours} height={240} series={[
                { metric: 'cpu_usage_percent', label: 'CPU' },
                { metric: 'memory_usage_percent', label: 'Memory' },
                { metric: 'storage_usage_percent', label: 'Storage' },
              ]} />
            </HistoryState>
          </Panel>
          <Panel title="Storage I/O" hint={`${chartCluster.name} · IOPS and latency`}>
            <HistoryState query={history}>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <div className="mb-1 text-xs font-medium text-text2">IOPS</div>
                  <HistoryChart data={history.data ?? []} hours={hours} height={210} series={[{ metric: 'iops' }]} />
                </div>
                <div>
                  <div className="mb-1 text-xs font-medium text-text2">Latency</div>
                  <HistoryChart data={history.data ?? []} hours={hours} height={210} series={[{ metric: 'io_latency_us' }]} />
                </div>
              </div>
            </HistoryState>
          </Panel>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Top VMs by CPU" hint="Current sample, powered-on VMs" className="xl:col-span-2">
          {topCpu.length ? (
            <BarList rows={topCpu} format={(v) => `${v}%`} onOpen={onOpen} color={seriesVar(0)} />
          ) : (
            <p className="text-sm text-muted">No VM CPU counters reported.</p>
          )}
        </Panel>
        <Panel title="VM power state" hint={plural(vms.length, 'VM')}>
          {power.length ? (
            <div className="flex items-center gap-4">
              <div className="h-40 w-40 shrink-0">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={power} dataKey="value" nameKey="name" innerRadius={45} outerRadius={70} paddingAngle={2} isAnimationActive={false}>
                      {power.map((p) => <Cell key={p.name} fill={p.color} />)}
                    </Pie>
                    <Tooltip contentStyle={chartTooltipStyle} />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <ul className="space-y-2 text-sm">
                {power.map((p) => (
                  <li key={p.name} className="flex items-center gap-2">
                    <span className="h-2.5 w-2.5 rounded-sm" style={{ background: p.color }} />
                    <span className="text-text2">{p.name}</span>
                    <span className="font-semibold tabular-nums">{p.value}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-sm text-muted">No virtual machines reported.</p>
          )}
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Host utilization" hint="CPU and memory per host" className="xl:col-span-2">
          {hostBars.length ? (
            <>
              <Legend items={[{ label: 'CPU', color: seriesVar(0) }, { label: 'Memory', color: seriesVar(1) }]} />
              <div className="mt-2" style={{ height: Math.max(160, hostBars.length * 46) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={hostBars} layout="vertical" margin={{ left: 8, right: 24 }} barGap={2}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.15} horizontal={false} />
                    <XAxis type="number" domain={[0, 100]} tickFormatter={(v) => `${v}%`} tick={{ fontSize: chartFont(11) }} />
                    <YAxis type="category" dataKey="name" width={130} tick={{ fontSize: chartFont(11) }} />
                    <Tooltip contentStyle={chartTooltipStyle} formatter={(v: number, k: string) => [`${v}%`, k === 'cpu' ? 'CPU' : 'Memory']} />
                    <Bar dataKey="cpu" fill={seriesVar(0)} radius={[0, 4, 4, 0]} barSize={12} isAnimationActive={false}
                      onClick={(d: { id?: string }) => d.id && onOpen(d.id)} cursor="pointer" />
                    <Bar dataKey="memory" fill={seriesVar(1)} radius={[0, 4, 4, 0]} barSize={12} isAnimationActive={false}
                      onClick={(d: { id?: string }) => d.id && onOpen(d.id)} cursor="pointer" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted">No hosts reported.</p>
          )}
        </Panel>
        <Panel title="Top VMs by IOPS" hint="Current sample">
          {topIops.length ? (
            <BarList rows={topIops} format={(v) => fmtNumber(v)} onOpen={onOpen} color={seriesVar(2)} />
          ) : (
            <p className="text-sm text-muted">No VM I/O counters reported.</p>
          )}
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel
          title="Active provider alerts"
          hint={active.length ? `${critical} critical · ${warning} warning` : undefined}
          right={active.length > topAlerts.length && (
            <button type="button" className="text-xs text-primary hover:underline" onClick={() => onTab('alerts')}>
              View all {active.length}
            </button>
          )}
        >
          {topAlerts.length ? (
            <ul className="divide-y divide-border">
              {topAlerts.map((a) => {
                const r = a.resource_id ? index.byId.get(a.resource_id) : undefined
                return (
                  <li key={a.id} className="flex items-start gap-3 py-2 first:pt-0 last:pb-0">
                    <SeverityBadge severity={a.severity} />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm">{a.title}</div>
                      <div className="text-xs text-muted">
                        {r ? (
                          <button type="button" className="text-primary hover:underline" onClick={() => onOpen(r.id)}>{r.name}</button>
                        ) : (
                          a.source_name || 'Prism Central'
                        )}
                        {a.created_at && ` · ${relativeTime(a.created_at)}`}
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="text-sm text-muted">No active provider alerts.</p>
          )}
        </Panel>
        <Panel title="Storage containers" hint="Usage of each container's capacity"
          right={storage.length > 6 && (
            <button type="button" className="text-xs text-primary hover:underline" onClick={() => onTab('storage')}>View all {storage.length}</button>
          )}>
          {storage.length ? (
            <ul className="space-y-3">
              {[...storage]
                .sort((a, b) => (b.metrics.storage_used_bytes ?? 0) - (a.metrics.storage_used_bytes ?? 0))
                .slice(0, 6)
                .map((s) => (
                  <li key={s.id}>
                    <button type="button" className="mb-1 flex w-full items-center justify-between gap-2 text-left text-sm hover:text-primary" onClick={() => onOpen(s.id)}>
                      <span className="truncate">{s.name}</span>
                      <span className="shrink-0 text-xs tabular-nums text-muted">
                        {fmtBytes(s.metrics.storage_used_bytes)} / {fmtBytes(s.metrics.storage_capacity_bytes)}
                      </span>
                    </button>
                    <Meter value={s.metrics.storage_usage_percent} />
                  </li>
                ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">
              <Database className="mr-1 inline h-4 w-4" />No storage reported.
            </p>
          )}
        </Panel>
      </div>
    </div>
  )
}

function Card4({ label, icon, value, detail }: { label: string; icon: React.ReactNode; value?: number; detail?: string }) {
  return (
    <Tile
      label={`${label} utilization`}
      value={value !== undefined ? fmtPct(value) : '—'}
      tone={value === undefined ? 'muted' : value >= 90 ? 'danger' : value >= 75 ? 'warning' : 'primary'}
      icon={icon}
      sub={
        <div className="space-y-1">
          {value !== undefined && (
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2">
              <div className={`h-full rounded-full ${value >= 90 ? 'bg-danger' : value >= 75 ? 'bg-warning' : 'bg-primary'}`} style={{ width: `${Math.min(100, value)}%` }} />
            </div>
          )}
          <div className="truncate">{detail ?? 'No capacity counters'}</div>
        </div>
      }
    />
  )
}

function BarList({ rows, format, onOpen, color }: {
  rows: { id: string; name: string; value: number }[]
  format: (v: number) => string
  onOpen: (id: string) => void
  color: string
}) {
  const max = Math.max(...rows.map((r) => r.value), 1)
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={r.id}>
          <button type="button" onClick={() => onOpen(r.id)} className="group grid w-full grid-cols-[minmax(0,10rem)_1fr_4.5rem] items-center gap-3 text-left text-sm">
            <span className="truncate group-hover:text-primary">{r.name}</span>
            <span className="h-2 overflow-hidden rounded-full bg-surface2">
              <span className="block h-full rounded-full" style={{ width: `${(r.value / max) * 100}%`, background: color }} />
            </span>
            <span className="text-right tabular-nums text-text2">{format(r.value)}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}
