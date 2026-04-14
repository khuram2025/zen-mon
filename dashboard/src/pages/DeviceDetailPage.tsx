import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Cpu, HardDrive, Pencil, Server, Trash2, Zap } from 'lucide-react'
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { toast } from '@/components/ui/Toast'
import { formatBps, relativeTime } from '@/lib/utils'

export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [editOpen, setEditOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)

  const { data: device, isLoading } = useQuery<any>({
    queryKey: ['device', id],
    queryFn: async () => (await api.get(`/devices/${id}`)).data,
    refetchInterval: 15_000,
    enabled: !!id,
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/devices/${id}`),
    onSuccess: () => {
      toast.success('Device deleted')
      qc.invalidateQueries({ queryKey: ['devices'] })
      navigate('/devices')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  if (isLoading || !device) {
    return <div className="text-muted">Loading device…</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <Link
            to="/devices"
            className="mb-2 inline-flex items-center gap-1 text-xs text-muted hover:text-primary"
          >
            <ArrowLeft className="h-3 w-3" />
            Back to devices
          </Link>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Server className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">{device.hostname}</h1>
              <div className="flex items-center gap-2 text-sm text-muted">
                <span className="font-mono">{device.ip_address}</span>
                <span>•</span>
                <span className="capitalize">{device.device_type}</span>
                {device.location && (
                  <>
                    <span>•</span>
                    <span>{device.location}</span>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={device.status} />
          <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
            <Pencil className="h-3.5 w-3.5" />
            Edit
          </Button>
          <Button variant="outline" size="sm" className="text-danger" onClick={() => setDelOpen(true)}>
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </Button>
        </div>
      </div>

      <DeviceFormDialog open={editOpen} onOpenChange={setEditOpen} device={device} />
      <ConfirmDialog
        open={delOpen}
        onOpenChange={setDelOpen}
        title="Delete device"
        description={
          <>
            Delete <span className="font-semibold text-text">{device.hostname}</span>?
            All history will be removed.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => del.mutate()}
      />

      {/* Quick info bar */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Info label="Vendor" value={device.vendor || '—'} />
        <Info label="Model" value={device.model || '—'} />
        <Info label="OS Version" value={device.os_version || '—'} />
        <Info label="Last RTT" value={device.last_rtt_ms != null ? `${device.last_rtt_ms.toFixed(1)} ms` : '—'} />
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          {device.snmp_enabled && <TabsTrigger value="interfaces">Interfaces</TabsTrigger>}
          {device.snmp_enabled && <TabsTrigger value="environment">Environment</TabsTrigger>}
          {device.snmp_enabled && <TabsTrigger value="inventory">Inventory</TabsTrigger>}
          {device.snmp_enabled && <TabsTrigger value="traps">Traps</TabsTrigger>}
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <OverviewTab device={device} />
        </TabsContent>
        {device.snmp_enabled && (
          <TabsContent value="interfaces">
            <InterfacesTab deviceId={id!} />
          </TabsContent>
        )}
        {device.snmp_enabled && (
          <TabsContent value="environment">
            <EnvironmentTab deviceId={id!} />
          </TabsContent>
        )}
        {device.snmp_enabled && (
          <TabsContent value="inventory">
            <InventoryTab deviceId={id!} />
          </TabsContent>
        )}
        {device.snmp_enabled && (
          <TabsContent value="traps">
            <TrapsTab deviceId={id!} />
          </TabsContent>
        )}
        <TabsContent value="settings">
          <SettingsTab device={device} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs font-medium uppercase tracking-wider text-muted">{label}</div>
        <div className="mt-1 truncate text-sm font-medium">{value}</div>
      </CardContent>
    </Card>
  )
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, any> = {
    up: 'success',
    down: 'danger',
    degraded: 'warning',
    unknown: 'outline',
    maintenance: 'info',
  }
  return <Badge variant={map[status] || 'outline'} className="h-6 px-3 text-sm capitalize">{status}</Badge>
}

// --- Overview tab ---

function OverviewTab({ device }: { device: any }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Device info</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Row k="Hostname" v={device.hostname} />
          <Row k="IP address" v={device.ip_address} />
          <Row k="Type" v={device.device_type} />
          <Row k="Group" v={device.group_name || '—'} />
          <Row k="Location" v={device.location || '—'} />
          <Row k="Last seen" v={relativeTime(device.last_seen)} />
          <Row k="Ping" v={device.ping_enabled ? `Enabled (${device.ping_interval}s)` : 'Disabled'} />
          <Row k="SNMP" v={device.snmp_enabled ? `Enabled (v${device.snmp_version})` : 'Disabled'} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>SNMP discovery</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Row k="Vendor" v={device.vendor || '—'} />
          <Row k="Model" v={device.model || '—'} />
          <Row k="OS version" v={device.os_version || '—'} />
          <Row k="sysObjectID" v={device.sys_object_id || '—'} mono />
          <Row k="Profile" v={device.profile_id ? 'Assigned' : 'Not matched'} />
        </CardContent>
      </Card>
    </div>
  )
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
      <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
      <span className={`text-right ${mono ? 'font-mono text-xs' : ''}`}>{v}</span>
    </div>
  )
}

// --- Interfaces tab ---

function InterfacesTab({ deviceId }: { deviceId: string }) {
  const { data: ifs } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000,
  })

  const { data: ifMetrics } = useQuery<Record<string, any[]>>({
    queryKey: ['device', deviceId, 'if-metrics'],
    queryFn: async () =>
      (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=1`)).data,
    refetchInterval: 30_000,
  })

  const lastBps = (idx: number) => {
    const series = ifMetrics?.[idx]
    if (!series?.length) return { in: 0, out: 0 }
    const last = series[series.length - 1]
    return { in: last.in_bps, out: last.out_bps }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Interfaces ({ifs?.length || 0})</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <THead>
            <Tr>
              <Th>#</Th>
              <Th>Name</Th>
              <Th>Speed</Th>
              <Th>Admin</Th>
              <Th>Oper</Th>
              <Th className="text-right">In</Th>
              <Th className="text-right">Out</Th>
              <Th>MAC</Th>
            </Tr>
          </THead>
          <TBody>
            {(ifs || []).map((i) => {
              const bps = lastBps(i.if_index)
              return (
                <Tr key={i.id}>
                  <Td className="font-mono text-xs text-muted">{i.if_index}</Td>
                  <Td className="font-medium">{i.if_name || i.if_descr}</Td>
                  <Td className="text-xs">{i.if_speed ? formatBps(Number(i.if_speed)) : '—'}</Td>
                  <Td>
                    <Badge variant={i.admin_status === 'up' ? 'success' : 'outline'}>
                      {i.admin_status || '—'}
                    </Badge>
                  </Td>
                  <Td>
                    <Badge variant={i.oper_status === 'up' ? 'success' : 'danger'}>
                      {i.oper_status || '—'}
                    </Badge>
                  </Td>
                  <Td className="text-right font-mono text-xs">{formatBps(bps.in)}</Td>
                  <Td className="text-right font-mono text-xs">{formatBps(bps.out)}</Td>
                  <Td className="font-mono text-xs text-muted">{i.mac_address || '—'}</Td>
                </Tr>
              )
            })}
            {(!ifs || ifs.length === 0) && (
              <Tr>
                <Td colSpan={8} className="text-center text-muted">
                  No interfaces discovered yet — waiting for next SNMP poll
                </Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}

// --- Environment tab ---

function EnvironmentTab({ deviceId }: { deviceId: string }) {
  const { data: metrics } = useQuery<Record<string, { unit: string; points: { ts: number; value: number }[] }>>({
    queryKey: ['device', deviceId, 'snmp-metrics'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-metrics?hours=6`)).data,
    refetchInterval: 30_000,
  })

  const cpu = metrics?.cpu
  const mem = metrics?.memory

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <MetricCard title="CPU" unit="%" icon={<Cpu className="h-4 w-4" />} data={cpu?.points} tone="info" />
      <MetricCard title="Memory" unit="%" icon={<HardDrive className="h-4 w-4" />} data={mem?.points} tone="warning" />
      {Object.entries(metrics || {})
        .filter(([k]) => k.startsWith('temperature_'))
        .map(([k, v]) => (
          <MetricCard key={k} title={k.replace('_', ' #')} unit="°C" icon={<Zap className="h-4 w-4" />} data={v.points} tone="danger" />
        ))}
      {!cpu && !mem && (
        <Card className="lg:col-span-2">
          <CardContent className="py-12 text-center text-muted">
            No environmental metrics yet — waiting for data from the SNMP poller.
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function MetricCard({
  title,
  unit,
  icon,
  data,
  tone,
}: {
  title: string
  unit: string
  icon: React.ReactNode
  data?: { ts: number; value: number }[]
  tone: 'info' | 'warning' | 'danger'
}) {
  const current = data?.length ? data[data.length - 1].value : null
  const stroke =
    tone === 'info' ? 'rgb(var(--info))' : tone === 'warning' ? 'rgb(var(--warning))' : 'rgb(var(--danger))'
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm capitalize">
          {icon}
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-3xl font-semibold">
          {current != null ? current.toFixed(1) : '—'}
          <span className="ml-1 text-sm text-muted">{unit}</span>
        </div>
        <div className="mt-3 h-24">
          {data && data.length > 1 ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data}>
                <Line type="monotone" dataKey="value" stroke={stroke} strokeWidth={2} dot={false} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(var(--surface))',
                    border: '1px solid rgb(var(--border))',
                    borderRadius: 6,
                    color: 'rgb(var(--text))',
                    fontSize: 12,
                  }}
                  labelFormatter={(ts: any) => new Date(ts).toLocaleTimeString()}
                  formatter={(v: any) => [`${v.toFixed(2)} ${unit}`, title]}
                />
                <XAxis dataKey="ts" hide />
                <YAxis hide domain={['auto', 'auto']} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center text-xs text-muted">
              Not enough samples yet
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

// --- Inventory tab ---

function InventoryTab({ deviceId }: { deviceId: string }) {
  const { data: entities } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'entities'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/entities`)).data,
  })
  const { data: sensors } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'sensors'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/sensors`)).data,
  })

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Hardware (ENTITY-MIB)</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <Tr>
                <Th>Index</Th>
                <Th>Class</Th>
                <Th>Name</Th>
                <Th>Serial</Th>
                <Th>Model</Th>
              </Tr>
            </THead>
            <TBody>
              {(entities || []).map((e) => (
                <Tr key={e.id}>
                  <Td className="font-mono text-xs">{e.ent_index}</Td>
                  <Td>{e.class}</Td>
                  <Td>{e.name}</Td>
                  <Td className="font-mono text-xs">{e.serial_number || '—'}</Td>
                  <Td>{e.model_name || '—'}</Td>
                </Tr>
              ))}
              {(!entities || entities.length === 0) && (
                <Tr>
                  <Td colSpan={5} className="text-center text-muted">
                    No ENTITY-MIB data (expected on Linux/net-snmp)
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Sensors (ENTITY-SENSOR-MIB)</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <Tr>
                <Th>Index</Th>
                <Th>Type</Th>
                <Th>Description</Th>
                <Th>Unit</Th>
              </Tr>
            </THead>
            <TBody>
              {(sensors || []).map((s) => (
                <Tr key={s.id}>
                  <Td className="font-mono text-xs">{s.sensor_index}</Td>
                  <Td>{s.sensor_type}</Td>
                  <Td>{s.description || '—'}</Td>
                  <Td>{s.unit || '—'}</Td>
                </Tr>
              ))}
              {(!sensors || sensors.length === 0) && (
                <Tr>
                  <Td colSpan={4} className="text-center text-muted">
                    No sensor data (expected on Linux/net-snmp)
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}

// --- Traps tab ---

function TrapsTab({ deviceId }: { deviceId: string }) {
  const { data: traps } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'traps'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=24&limit=50`)).data,
    refetchInterval: 10_000,
  })

  const variant = (sev: string): any =>
    sev === 'critical' ? 'danger' : sev === 'warning' ? 'warning' : 'info'

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent traps — 24h</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <THead>
            <Tr>
              <Th>Time</Th>
              <Th>Source</Th>
              <Th>Severity</Th>
              <Th>Trap OID</Th>
              <Th>Message</Th>
            </Tr>
          </THead>
          <TBody>
            {(traps || []).map((t, i) => (
              <Tr key={i}>
                <Td className="text-xs text-muted">{relativeTime(t.timestamp)}</Td>
                <Td className="font-mono text-xs">{t.source_ip}</Td>
                <Td>
                  <Badge variant={variant(t.severity)}>{t.severity}</Badge>
                </Td>
                <Td className="font-mono text-xs">{t.trap_oid}</Td>
                <Td className="text-sm">{t.message}</Td>
              </Tr>
            ))}
            {(!traps || traps.length === 0) && (
              <Tr>
                <Td colSpan={5} className="text-center text-muted">
                  No traps in the last 24 hours
                </Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}

// --- Settings tab ---

function SettingsTab({ device }: { device: any }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Device settings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="space-y-3 rounded-md border border-border p-4">
            <h3 className="text-sm font-semibold">Ping</h3>
            <Row k="Enabled" v={device.ping_enabled ? 'Yes' : 'No'} />
            <Row k="Interval" v={`${device.ping_interval}s`} />
          </div>
          <div className="space-y-3 rounded-md border border-border p-4">
            <h3 className="text-sm font-semibold">SNMP</h3>
            <Row k="Enabled" v={device.snmp_enabled ? 'Yes' : 'No'} />
            <Row k="Version" v={`v${device.snmp_version}`} />
            <Row k="Port" v={String(device.snmp_port || 161)} />
            {device.snmp_version === '3' && (
              <>
                <Row k="Username" v={device.snmp_v3_username || '—'} />
                <Row k="Auth" v={device.snmp_auth_protocol || '—'} />
                <Row k="Privacy" v={device.snmp_priv_protocol || '—'} />
                <Row k="Auth configured" v={device.snmp_auth_configured ? 'Yes' : 'No'} />
                <Row k="Priv configured" v={device.snmp_priv_configured ? 'Yes' : 'No'} />
              </>
            )}
          </div>
        </div>
        <p className="text-xs text-muted">
          To edit credentials or monitoring settings, use the device edit form. Passphrases are write-only and cannot be viewed after saving.
        </p>
      </CardContent>
    </Card>
  )
}
