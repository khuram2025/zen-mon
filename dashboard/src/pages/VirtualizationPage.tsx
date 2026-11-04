import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  ExternalLink,
  Plus,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Link } from 'react-router-dom'
import { chartFont, seriesVar } from '@/lib/chartTheme'
import { regionalDate } from '@/lib/regionalTime'
import { Input } from '@/components/ui/Input'
import { Card } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'

const providers = {
  nutanix_pc: 'Nutanix Prism Central',
  nutanix_pe: 'Nutanix Prism Element',
  vmware: 'VMware vCenter / ESXi',
  hyperv: 'Microsoft Hyper-V',
}
type Provider = keyof typeof providers
type Resource = {
  id: string
  external_id: string
  kind: string
  name: string
  parent_id?: string
  health: string
  power_state?: string
  metrics: Record<string, number>
  url?: string
}
type Alert = {
  id: string
  title: string
  severity: string
  resolved: boolean
  acknowledged: boolean
  resource_id?: string
  resource_url?: string
  url?: string
}
type Summary = {
  version?: string
  counts?: Record<string, number>
  warnings?: string[]
  capabilities?: Record<string, boolean | number>
  alert_count?: number
}
type Connector = {
  id: string
  name: string
  provider: Provider
  endpoint: string
  tags: string[]
  enabled: boolean
  interval_seconds: number
  status: string
  stale: boolean
  last_error?: string
  last_success_at?: string
  summary: Summary
  resources?: Resource[]
  alerts?: Alert[]
}
type Connection = {
  provider: Provider
  endpoint: string
  username: string
  secret: string
  auth_type: 'password' | 'api_key'
  ca_pem: string
  api_version: string
}
const freshConnection = (): Connection => ({
  provider: 'nutanix_pc',
  endpoint: '',
  username: '',
  secret: '',
  auth_type: 'password',
  ca_pem: '',
  api_version: 'v4.0',
})
const selectStyle =
  'h-9 rounded-md border border-border bg-surface px-3 text-sm'
const percent = (v?: number) =>
  v === undefined ? 'Unknown' : `${v.toFixed(1)}%`
function metricLabel(key: string) {
  const names: Record<string, string> = {
    cpu_usage_percent: 'CPU utilization',
    memory_usage_percent: 'Memory utilization',
    storage_usage_percent: 'Storage utilization',
    cpu_capacity_hz: 'CPU capacity (Hz)',
    cpu_used_hz: 'CPU used (Hz)',
    cpu_cores: 'CPU cores',
    health_score: 'Health score',
    memory_capacity_bytes: 'Memory capacity',
    memory_used_bytes: 'Memory used',
    memory_assigned_bytes: 'Assigned memory',
    memory_demand_bytes: 'Memory demand',
    memory_demand_percent: 'Memory demand / assigned',
    memory_balloon_bytes: 'Ballooned memory',
    storage_capacity_bytes: 'Storage capacity',
    storage_used_bytes: 'Storage allocation used',
    io_bandwidth_bytes_per_second: 'I/O throughput (bytes/s)',
    io_latency_us: 'I/O latency (µs)',
    read_latency_us: 'Read latency (µs)',
    write_latency_us: 'Write latency (µs)',
    network_bytes_per_second: 'Network throughput (bytes/s)',
    uptime_seconds: 'Uptime (seconds)',
    cpu_ready_percent: 'CPU ready',
    cpu_ready_ms: 'CPU ready (ms / sample)',
    iops: 'IOPS',
    read_iops: 'Read IOPS',
    write_iops: 'Write IOPS',
  }
  return names[key] || key.replaceAll('_', ' ')
}
function value(v: number, key: string) {
  if (key.endsWith('_bytes')) {
    const i = v > 0 ? Math.min(4, Math.floor(Math.log(v) / Math.log(1024))) : 0
    return `${(v / 1024 ** i).toFixed(1)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][i]}`
  }
  return key.endsWith('_percent')
    ? percent(v)
    : v.toLocaleString(undefined, { maximumFractionDigits: 2 })
}
function ProviderLink({
  url,
  children,
}: {
  url?: string
  children: React.ReactNode
}) {
  if (!url || !url.startsWith('https://')) return null
  return (
    <a
      className="inline-flex items-center gap-1 text-primary"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
      <ExternalLink className="h-3 w-3" />
    </a>
  )
}

export function VirtualizationPage() {
  const canManage = useCan()('system.admin')
  const qc = useQueryClient()
  const [selected, setSelected] = useState('')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Connector | null>(null)
  const [connection, setConnection] = useState<Connection>(freshConnection)
  const [name, setName] = useState('')
  const [tags, setTags] = useState('')
  const [interval, setInterval] = useState(300)
  const [test, setTest] = useState<(Summary & { test_token: string }) | null>(
    null,
  )
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [remove, setRemove] = useState(false)
  const [search, setSearch] = useState('')
  const [kind, setKind] = useState('all')
  const [resourceId, setResourceId] = useState('')
  const [metric, setMetric] = useState('cpu_usage_percent')
  const [page, setPage] = useState(0)
  const list = useQuery({
    queryKey: ['virtualization'],
    queryFn: () =>
      api.get<Connector[]>('/virtualization/connectors').then((r) => r.data),
    refetchInterval: 15000,
  })
  const selectedId = selected || list.data?.[0]?.id || ''
  const detail = useQuery({
    queryKey: ['virtualization', selectedId],
    queryFn: () =>
      api
        .get<Connector>(`/virtualization/connectors/${selectedId}`)
        .then((r) => r.data),
    enabled: !!selectedId,
    refetchInterval: 15000,
  })
  const current = detail.data
  const resources = current?.resources || []
  const resource = resources.find((r) => r.id === resourceId)
  const history = useQuery({
    queryKey: ['virtualization', selectedId, resourceId, 'history'],
    queryFn: () =>
      api
        .get<
          Array<{ time: string; metric: string; value: number }>
        >(`/virtualization/connectors/${selectedId}/metrics`, { params: { resource_id: resourceId } })
        .then((r) => r.data),
    enabled: !!resource,
    refetchInterval: 60000,
  })
  const filtered = resources.filter(
    (r) =>
      (kind === 'all' || r.kind === kind) &&
      `${r.name} ${r.external_id}`.toLowerCase().includes(search.toLowerCase()),
  )
  const refresh = () => qc.invalidateQueries({ queryKey: ['virtualization'] })
  function change(values: Partial<Connection>) {
    setConnection((c) => ({ ...c, ...values }))
    setTest(null)
    setError('')
  }
  async function act(label: string, action: () => Promise<unknown>) {
    setBusy(label)
    setError('')
    try {
      await action()
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy('')
    }
  }
  function openNew() {
    setEditing(null)
    setConnection(freshConnection())
    setName('')
    setTags('')
    setInterval(300)
    setTest(null)
    setError('')
    setFormOpen(true)
  }
  async function openEdit() {
    if (!current) return
    await act('edit', async () => {
      const r = await api.get<Connection>(
        `/virtualization/connectors/${current.id}/configuration`,
      )
      setConnection({ ...r.data, secret: '' })
      setEditing(current)
      setName(current.name)
      setTags(current.tags.join(', '))
      setInterval(current.interval_seconds)
      setTest(null)
      setFormOpen(true)
    })
  }
  async function save() {
    if (!test) return
    await act('save', async () => {
      const payload = {
        name,
        connection,
        interval_seconds: interval,
        tags: tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        test_token: test.test_token,
      }
      const r = editing
        ? await api.put<Connector>(
            `/virtualization/connectors/${editing.id}`,
            payload,
          )
        : await api.post<Connector>('/virtualization/connectors', payload)
      setSelected(r.data.id)
      setFormOpen(false)
      setConnection(freshConnection())
      setTest(null)
      await refresh()
    })
  }
  return (
    <div className="space-y-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to="/servers/inventory" className="mb-1 inline-block text-xs text-muted hover:text-primary">All Servers</Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Activity />
            Virtualization
          </h1>
          <p className="mt-1 text-sm text-muted">
            Clusters, hypervisors, virtual machines and storage from
            authenticated provider APIs.
          </p>
        </div>
        {canManage && (
          <Button onClick={openNew}>
            <Plus />
            Add connector
          </Button>
        )}
      </div>
      {(error || list.error || detail.error) && (
        <div
          role="alert"
          className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger"
        >
          {error || apiErrorMessage(list.error || detail.error)}
        </div>
      )}
      {list.isLoading && <p className="text-muted">Loading connectors…</p>}
      {!list.isLoading && !list.error && !list.data?.length && (
        <Card className="p-10 text-center">
          <ShieldCheck className="mx-auto mb-3 h-10 w-10 text-primary" />
          <h2 className="font-semibold">
            Connect your virtualization platform
          </h2>
          <p className="mt-2 text-sm text-muted">
            Add Prism Central, Prism Element, VMware or Hyper-V. Test API access
            and certificate trust before saving.
          </p>
        </Card>
      )}
      {!!list.data?.length && (
        <>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {list.data.map((c) => (
              <button
                key={c.id}
                onClick={() => {
                  setSelected(c.id)
                  setResourceId('')
                  setPage(0)
                }}
                className={`rounded-lg border p-4 text-left ${selectedId === c.id ? 'border-primary bg-primary/5' : 'border-border bg-surface'}`}
              >
                <div className="font-semibold">{c.name}</div>
                <div className="mt-1 text-xs text-muted">
                  {providers[c.provider]}
                </div>
                <div className="mt-3 text-sm">
                  {!c.enabled
                    ? 'Paused'
                    : c.stale
                      ? 'Stale / no current data'
                      : c.status === 'partial'
                        ? 'Partial coverage'
                        : c.status === 'ok'
                          ? 'Collecting'
                          : 'Pending'}
                </div>
              </button>
            ))}
          </div>
          {current && (
            <>
              <Card className="space-y-3 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">{current.name}</h2>
                    <p className="text-sm text-muted">
                      {current.endpoint} ·{' '}
                      {current.summary.version || 'Awaiting first collection'}
                    </p>
                    <p className="text-xs text-muted">
                      Last successful collection:{' '}
                      {current.last_success_at
                        ? regionalDate(new Date(current.last_success_at), 'datetime')
                        : 'Never'}
                    </p>
                  </div>
                  {canManage && (
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        disabled={!!busy || !current.enabled}
                        onClick={() =>
                          act('poll', async () => {
                            await api.post(
                              `/virtualization/connectors/${current.id}/poll`,
                            )
                            await refresh()
                          })
                        }
                      >
                        <RefreshCw />
                        Poll now
                      </Button>
                      <Button
                        variant="outline"
                        disabled={!!busy}
                        onClick={openEdit}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="outline"
                        disabled={!!busy}
                        onClick={() =>
                          act('toggle', async () => {
                            await api.patch(
                              `/virtualization/connectors/${current.id}`,
                              { enabled: !current.enabled },
                            )
                            await refresh()
                          })
                        }
                      >
                        {current.enabled ? 'Pause' : 'Resume'}
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={!!busy}
                        onClick={() => setRemove(true)}
                      >
                        Delete
                      </Button>
                    </div>
                  )}
                </div>
                {current.stale && (
                  <p role="status" className="text-sm text-warning">
                    Data is stale or unavailable. Displayed resource values are
                    from the last successful collection.
                  </p>
                )}
                {current.last_error && (
                  <p className="text-sm text-danger">{current.last_error}</p>
                )}
                {!!current.summary.warnings?.length && (
                  <details>
                    <summary className="cursor-pointer text-sm text-warning">
                      Coverage notes ({current.summary.warnings.length})
                    </summary>
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted">
                      {current.summary.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </details>
                )}
                <div className="flex flex-wrap gap-5 text-sm">
                  {Object.entries(current.summary.counts || {}).map(
                    ([k, n]) => (
                      <span key={k}>
                        <strong>{n}</strong>{' '}
                        {k === 'storage' ? 'storage resources' : `${k}s`}
                      </span>
                    ),
                  )}
                  <span>
                    <strong>
                      {current.alerts?.filter((a) => !a.resolved).length || 0}
                    </strong>{' '}
                    active provider alerts
                  </span>
                </div>
              </Card>
              <Card className="overflow-hidden">
                <div className="flex flex-wrap gap-3 p-4">
                  <Input
                    className="max-w-sm"
                    aria-label="Search resources"
                    placeholder="Search name or provider ID"
                    value={search}
                    onChange={(e) => {
                      setSearch(e.target.value)
                      setPage(0)
                    }}
                  />
                  <select
                    aria-label="Resource type"
                    className={selectStyle}
                    value={kind}
                    onChange={(e) => {
                      setKind(e.target.value)
                      setPage(0)
                    }}
                  >
                    {['all', 'cluster', 'host', 'vm', 'storage'].map((k) => (
                      <option key={k} value={k}>
                        {k === 'all' ? 'All resources' : k}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="border-y border-border bg-surface2 text-xs text-muted">
                      <tr>
                        {[
                          'Resource',
                          'Type / parent',
                          'Health / power',
                          'CPU',
                          'Memory',
                          'Storage',
                          'Provider',
                        ].map((h) => (
                          <th key={h} className="p-3">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.slice(page * 50, (page + 1) * 50).map((r) => (
                        <tr key={r.id} className="border-b border-border">
                          <td className="p-3">
                            <button
                              className="text-primary"
                              onClick={() => {
                                setResourceId(r.id)
                                setMetric('cpu_usage_percent')
                              }}
                            >
                              {r.name}
                            </button>
                          </td>
                          <td className="p-3">
                            {r.kind}
                            <div className="text-xs text-muted">
                              {resources.find((p) => p.id === r.parent_id)
                                ?.name || '—'}
                            </div>
                          </td>
                          <td className="p-3">
                            {current.stale ? 'Stale' : r.health}
                            <div className="text-xs text-muted">
                              {r.power_state}
                            </div>
                          </td>
                          <td className="p-3">
                            {percent(r.metrics.cpu_usage_percent)}
                          </td>
                          <td className="p-3">
                            {percent(r.metrics.memory_usage_percent)}
                            <div className="text-xs text-muted">
                              {r.metrics.memory_capacity_bytes !== undefined &&
                                value(
                                  r.metrics.memory_capacity_bytes,
                                  'memory_capacity_bytes',
                                )}
                            </div>
                          </td>
                          <td className="p-3">
                            {percent(r.metrics.storage_usage_percent)}
                            <div className="text-xs text-muted">
                              {r.metrics.storage_capacity_bytes !== undefined &&
                                value(
                                  r.metrics.storage_capacity_bytes,
                                  'storage_capacity_bytes',
                                )}
                            </div>
                          </td>
                          <td className="p-3">
                            <ProviderLink url={r.url}>Open</ProviderLink>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!filtered.length && (
                  <p className="p-6 text-sm text-muted">
                    No resources match. Discovery begins after the first
                    successful poll.
                  </p>
                )}
                <div className="flex items-center justify-between p-3 text-sm text-muted">
                  <span>
                    {filtered.length} resources · page {page + 1}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page === 0}
                      onClick={() => setPage((p) => p - 1)}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={(page + 1) * 50 >= filtered.length}
                      onClick={() => setPage((p) => p + 1)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              </Card>
              {resource && (
                <Card className="space-y-4 p-4">
                  <div className="flex items-center justify-between">
                    <h2 className="font-semibold">
                      {resource.name} · last 24 hours
                    </h2>
                    <Button variant="ghost" onClick={() => setResourceId('')}>
                      Close
                    </Button>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-5">
                    {Object.entries(resource.metrics).map(([k, v]) => (
                      <div key={k} className="rounded-md bg-surface2 p-3">
                        <div className="text-xs text-muted">
                          {metricLabel(k)}
                        </div>
                        <div className="mt-1 font-medium">{value(v, k)}</div>
                      </div>
                    ))}
                  </div>
                  <select
                    aria-label="History metric"
                    className={selectStyle}
                    value={metric}
                    onChange={(e) => setMetric(e.target.value)}
                  >
                    {Array.from(
                      new Set([
                        'cpu_usage_percent',
                        ...Object.keys(resource.metrics),
                      ]),
                    ).map((k) => (
                      <option key={k} value={k}>
                        {metricLabel(k)}
                      </option>
                    ))}
                  </select>
                  {history.error ? (
                    <p className="text-danger">
                      {apiErrorMessage(history.error)}
                    </p>
                  ) : history.isLoading ? (
                    <p>Loading history…</p>
                  ) : !history.data?.some((p) => p.metric === metric) ? (
                    <p className="text-sm text-muted">
                      No historical samples for this metric.
                    </p>
                  ) : (
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={history.data.filter((p) => p.metric === metric)}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.15} />
                          <XAxis
                            tick={{ fontSize: chartFont(11) }}
                            dataKey="time"
                            tickFormatter={(t) =>
                              regionalDate(new Date(t), 'time', [], {
                                hour: '2-digit',
                                minute: '2-digit',
                              })
                            }
                          />
                          <YAxis tick={{ fontSize: chartFont(11) }} />
                          <Tooltip
                            labelFormatter={(t) => regionalDate(new Date(t), 'datetime')}
                          />
                          <Line
                            type="monotone"
                            dataKey="value"
                            stroke={seriesVar(0)}
                            dot={false}
                            connectNulls={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                </Card>
              )}
              <Card className="p-4">
                <h2 className="mb-3 font-semibold">Provider alerts</h2>
                <p className="mb-3 text-xs text-muted">
                  Read-only provider state. Acknowledgment and resolution remain
                  in Prism, vSphere or Hyper-V. Hyper-V events cover the latest
                  24 hours.
                </p>
                <div className="max-h-96 space-y-2 overflow-auto">
                  {current.alerts?.map((a) => (
                    <div
                      key={a.id}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3 text-sm"
                    >
                      <div>
                        <span className="mr-2 font-medium">
                          {a.resolved ? 'Resolved' : a.severity}
                        </span>
                        {a.title}
                        {a.acknowledged && (
                          <span className="ml-2 text-xs text-muted">
                            Acknowledged
                          </span>
                        )}
                      </div>
                      <div className="flex gap-3">
                        {a.resource_id && (
                          <button
                            className="text-primary"
                            onClick={() => setResourceId(a.resource_id!)}
                          >
                            Resource
                          </button>
                        )}
                        <ProviderLink url={a.resource_url}>
                          Provider resource
                        </ProviderLink>
                        <ProviderLink url={a.url}>Alert</ProviderLink>
                      </div>
                    </div>
                  ))}
                  {!current.alerts?.length && (
                    <p className="text-sm text-muted">
                      {current.summary.capabilities?.alerts
                        ? 'No provider alerts returned.'
                        : 'Alert coverage is not available yet.'}
                    </p>
                  )}
                </div>
              </Card>
            </>
          )}
        </>
      )}
      <Dialog
        open={formOpen}
        onOpenChange={(o) => {
          if (!busy) {
            setFormOpen(o)
            if (!o) {
              setConnection(freshConnection())
              setTest(null)
            }
          }
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-auto">
          <DialogHeader>
            <DialogTitle>
              {editing
                ? 'Edit virtualization connector'
                : 'Add virtualization connector'}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="flex items-center gap-2 text-sm text-muted">
              <ShieldCheck className="h-4 w-4" />
              TLS certificate and hostname validation are always enabled.
            </p>
            <fieldset disabled={!!busy} className="space-y-4">
              <label className="block space-y-1 text-sm">
                Name
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={150}
                />
              </label>
              <label className="block space-y-1 text-sm">
                Platform
                <select
                  className={`${selectStyle} w-full`}
                  value={connection.provider}
                  disabled={!!editing}
                  onChange={(e) =>
                    change({
                      provider: e.target.value as Provider,
                      auth_type: 'password',
                    })
                  }
                >
                  {Object.entries(providers).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-1 text-sm">
                HTTPS endpoint
                <Input
                  placeholder={
                    connection.provider === 'hyperv'
                      ? 'https://hyperv.example.org:5986'
                      : connection.provider === 'vmware'
                        ? 'https://vcenter.example.org'
                        : 'https://prism.example.org:9440'
                  }
                  value={connection.endpoint}
                  disabled={!!editing}
                  onChange={(e) => change({ endpoint: e.target.value })}
                />
              </label>
              {connection.provider === 'nutanix_pc' && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="block space-y-1 text-sm">
                    GA API version
                    <select
                      className={`${selectStyle} w-full`}
                      value={connection.api_version}
                      onChange={(e) => change({ api_version: e.target.value })}
                    >
                      {['v4.0', 'v4.1', 'v4.2', 'v4.3'].map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  </label>
                  <label className="block space-y-1 text-sm">
                    Credential type
                    <select
                      className={`${selectStyle} w-full`}
                      value={connection.auth_type}
                      onChange={(e) =>
                        change({
                          auth_type: e.target.value as 'password' | 'api_key',
                        })
                      }
                    >
                      <option value="password">Read-only account</option>
                      <option value="api_key">
                        IAM service account API key
                      </option>
                    </select>
                  </label>
                </div>
              )}
              {connection.auth_type === 'password' && (
                <label className="block space-y-1 text-sm">
                  Monitoring username
                  <Input
                    autoComplete="off"
                    value={connection.username}
                    onChange={(e) => change({ username: e.target.value })}
                  />
                </label>
              )}
              <label className="block space-y-1 text-sm">
                {connection.auth_type === 'api_key' ? 'API key' : 'Password'}
                {editing && ' (re-enter to test changes)'}
                <Input
                  type="password"
                  autoComplete="new-password"
                  value={connection.secret}
                  onChange={(e) => change({ secret: e.target.value })}
                />
              </label>
              <label className="block space-y-1 text-sm">
                Private CA certificate chain (PEM, optional)
                <textarea
                  className="min-h-24 w-full rounded-md border border-border bg-surface p-2 font-mono text-xs"
                  placeholder="-----BEGIN CERTIFICATE-----"
                  value={connection.ca_pem}
                  onChange={(e) => change({ ca_pem: e.target.value })}
                />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block space-y-1 text-sm">
                  Polling interval (seconds)
                  <Input
                    type="number"
                    min={60}
                    max={3600}
                    value={interval}
                    onChange={(e) => setInterval(Number(e.target.value))}
                  />
                </label>
                <label className="block space-y-1 text-sm">
                  Visibility tags (comma separated)
                  <Input
                    value={tags}
                    onChange={(e) => setTags(e.target.value)}
                  />
                </label>
              </div>
            </fieldset>
            {connection.provider === 'hyperv' && (
              <p className="text-xs text-muted">
                Requires the ZenPlusMonitoring JEA module on each host and a
                trusted WinRM HTTPS listener. Use a dedicated account authorized
                only for this endpoint.
              </p>
            )}
            {connection.provider === 'nutanix_pe' && (
              <p className="text-xs text-muted">
                Element v2 coverage depends on AOS. VM performance counters may
                require Prism Central v4.
              </p>
            )}
            {error && (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
            {test && (
              <div className="space-y-2 rounded-md border border-primary/30 bg-primary/5 p-3 text-sm">
                <p className="font-medium">
                  Authenticated collection succeeded · {test.version}
                </p>
                <p>
                  {Object.entries(test.counts || {})
                    .map(([k, v]) => `${v} ${k}`)
                    .join(' · ')}
                </p>
                {test.warnings?.map((w, i) => (
                  <p key={i} className="text-warning">
                    {w}
                  </p>
                ))}
                <p className="text-xs text-muted">
                  Review coverage before saving. Test validity: 10 minutes.
                </p>
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                disabled={!!busy || !connection.endpoint || !connection.secret}
                onClick={() => {
                  setTest(null)
                  act('test', async () => {
                    const r = await api.post(
                      '/virtualization/test',
                      connection,
                      { timeout: 310000 },
                    )
                    setTest(r.data)
                  })
                }}
              >
                {busy === 'test'
                  ? 'Testing discovery and metrics…'
                  : 'Test connection'}
              </Button>
              <Button
                disabled={
                  !!busy ||
                  !test ||
                  !name.trim() ||
                  interval < 60 ||
                  interval > 3600
                }
                onClick={save}
              >
                {busy === 'save' ? 'Saving…' : 'Save connector'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={remove}
        onOpenChange={setRemove}
        title="Delete virtualization connector?"
        description="Stops collection and deletes its stored credential and current inventory. Metric history expires after 30 days."
        destructive
        loading={!!busy}
        onConfirm={() =>
          act('delete', async () => {
            await api.delete(`/virtualization/connectors/${selectedId}`)
            setRemove(false)
            setSelected('')
            setResourceId('')
            await refresh()
          })
        }
      />
    </div>
  )
}
