import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { MoreVertical, Pencil, Plus, Search, Shield, Trash2, Wifi } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { StatusDot, deviceStatusKind } from '@/components/ui/StatusDot'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { toast } from '@/components/ui/Toast'
import { relativeTime } from '@/lib/utils'

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  status: string
  last_seen: string | null
  last_rtt_ms: number | null
  snmp_enabled: boolean
  vendor: string | null
  model: string | null
  os_version: string | null
}

export function DevicesPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const initial = params.get('search') || ''
  const [search, setSearch] = useState(initial)
  const [statusFilter, setStatusFilter] = useState<string>('')

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Device | null>(null)
  const [deleting, setDeleting] = useState<Device | null>(null)

  const { data, isLoading } = useQuery<{ data: Device[]; meta: any }>({
    queryKey: ['devices', 'list', { search, statusFilter }],
    queryFn: async () => {
      const qs: string[] = ['limit=200']
      if (search) qs.push(`search=${encodeURIComponent(search)}`)
      if (statusFilter) qs.push(`status=${encodeURIComponent(statusFilter)}`)
      return (await api.get(`/devices?${qs.join('&')}`)).data
    },
    refetchInterval: 15_000,
  })

  const devices = data?.data || []
  const counts = useMemo(() => {
    return {
      total: devices.length,
      up: devices.filter((d) => d.status === 'up').length,
      down: devices.filter((d) => d.status === 'down').length,
      degraded: devices.filter((d) => d.status === 'degraded').length,
      snmp: devices.filter((d) => d.snmp_enabled).length,
    }
  }, [devices])

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/devices/${id}`),
    onSuccess: () => {
      toast.success('Device deleted')
      qc.invalidateQueries({ queryKey: ['devices'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Devices</h1>
          <p className="text-xs text-muted">
            {counts.total} monitored • {counts.snmp} with SNMP
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          Add device
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Total" value={counts.total} />
        <StatCard label="Online" value={counts.up} tone="success" />
        <StatCard label="Offline" value={counts.down} tone={counts.down > 0 ? 'danger' : undefined} />
        <StatCard label="Degraded" value={counts.degraded} tone={counts.degraded > 0 ? 'warning' : undefined} />
      </div>

      <Card>
        <CardContent className="space-y-3 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative max-w-md flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input
                placeholder="Search hostname, IP, description…"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value)
                  if (e.target.value) setParams({ search: e.target.value })
                  else setParams({})
                }}
                className="pl-8"
              />
            </div>
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
              {[
                { label: 'All', value: '' },
                { label: 'Up', value: 'up' },
                { label: 'Down', value: 'down' },
                { label: 'Degraded', value: 'degraded' },
                { label: 'Unknown', value: 'unknown' },
              ].map((f) => (
                <button
                  key={f.value}
                  onClick={() => setStatusFilter(f.value)}
                  className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                    statusFilter === f.value
                      ? 'bg-surface text-text shadow-sm'
                      : 'text-muted hover:text-text'
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>

          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-8"></Th>
                  <Th>Hostname</Th>
                  <Th>IP address</Th>
                  <Th>Protocol</Th>
                  <Th>Vendor / Model</Th>
                  <Th>Type</Th>
                  <Th>Location</Th>
                  <Th>RTT</Th>
                  <Th>Last seen</Th>
                  <Th className="w-10"></Th>
                </Tr>
              </THead>
              <TBody>
                {isLoading && (
                  <Tr>
                    <Td colSpan={10}>
                      <SkeletonTable rows={5} cols={7} />
                    </Td>
                  </Tr>
                )}
                {!isLoading && devices.length === 0 && (
                  <Tr>
                    <Td colSpan={10} className="py-10 text-center text-muted">
                      No devices found
                    </Td>
                  </Tr>
                )}
                {devices.map((d) => (
                  <Tr key={d.id}>
                    <Td>
                      <StatusDot status={deviceStatusKind(d.status)} pulse={d.status === 'up'} />
                    </Td>
                    <Td>
                      <Link
                        to={`/devices/${d.id}`}
                        className="font-medium text-text hover:text-primary hover:underline"
                      >
                        {d.hostname}
                      </Link>
                    </Td>
                    <Td className="font-mono text-xs text-muted">{d.ip_address}</Td>
                    <Td>
                      <div className="flex items-center gap-1">
                        <Badge variant="info" className="gap-1">
                          <Wifi className="h-3 w-3" />
                          PING
                        </Badge>
                        {d.snmp_enabled && (
                          <Badge variant="success" className="gap-1">
                            <Shield className="h-3 w-3" />
                            SNMP
                          </Badge>
                        )}
                      </div>
                    </Td>
                    <Td className="text-sm">
                      {d.vendor || d.model ? (
                        <div>
                          <div className="font-medium leading-tight">{d.vendor || '—'}</div>
                          {d.model && <div className="text-[11px] text-muted">{d.model}</div>}
                        </div>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </Td>
                    <Td className="text-sm capitalize">{d.device_type}</Td>
                    <Td className="text-sm">{d.location || '—'}</Td>
                    <Td className="font-mono text-xs">
                      {d.last_rtt_ms != null ? `${d.last_rtt_ms.toFixed(1)}ms` : '—'}
                    </Td>
                    <Td className="text-xs text-muted">{relativeTime(d.last_seen)}</Td>
                    <Td>
                      <div className="flex gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => { setEditing(d); setFormOpen(true) }}
                          title="Edit"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          onClick={() => setDeleting(d)}
                          title="Delete"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <DeviceFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        device={editing}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete device"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.hostname}</span>?
            This also removes its metrics history.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'success' | 'warning' | 'danger'
}) {
  const color =
    tone === 'success'
      ? 'text-success'
      : tone === 'warning'
        ? 'text-warning'
        : tone === 'danger'
          ? 'text-danger'
          : 'text-text'
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}
