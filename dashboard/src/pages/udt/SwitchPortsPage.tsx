import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowUpFromLine, Power, PowerOff, Server } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage } from '@/lib/utils'
import { udtApi } from './api'
import type { CapacityRow, UdtPort } from './types'
import { relTime, speedLabel } from './helpers'

function CapacityBar({ used, total }: { used: number; total: number }) {
  const pct = total ? Math.round((used / total) * 100) : 0
  const tone = pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-success'
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-surface3">
        <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-muted">{used}/{total}</span>
    </div>
  )
}

export function SwitchPortsPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const selected = params.get('device')
  const [confirm, setConfirm] = useState<{ port: UdtPort; action: 'shutdown' | 'enable' } | null>(null)

  const capacity = useQuery({
    queryKey: ['udt', 'capacity'],
    queryFn: () => udtApi.capacity(),
    refetchInterval: 30_000,
  })

  const devices = capacity.data?.data || []
  const activeDevice = selected || devices[0]?.id

  const ports = useQuery({
    queryKey: ['udt', 'device-ports', activeDevice],
    queryFn: () => udtApi.devicePorts(activeDevice!),
    refetchInterval: 20_000,
    enabled: !!activeDevice,
  })

  const action = useMutation({
    mutationFn: ({ port, act }: { port: UdtPort; act: string }) =>
      udtApi.portAction(activeDevice!, port.if_index, act),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ['udt', 'device-ports', activeDevice] })
      toast.success(`Port ${v.act === 'shutdown' ? 'shut down' : v.act === 'enable' ? 'enabled' : 'updated'}`)
    },
    onError: (e: any) => toast.error('Action failed', apiErrorMessage(e)),
  })

  const overrideMut = useMutation({
    mutationFn: ({ port, value }: { port: UdtPort; value: string }) =>
      udtApi.updatePort(activeDevice!, port.if_index, { uplink_override: value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['udt', 'device-ports', activeDevice] })
      toast.success('Port classification updated')
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[300px_minmax(0,1fr)]">
      {/* Device list / capacity rail */}
      <div className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-muted">Switches</div>
        {capacity.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : devices.length === 0 ? (
          <Card><CardContent className="p-4 text-xs text-muted">No SNMP switches with UDT data yet.</CardContent></Card>
        ) : (
          devices.map((d: CapacityRow) => (
            <button
              key={d.id}
              onClick={() => setParams((p) => { p.set('device', d.id); return p }, { replace: true })}
              className={`w-full rounded-lg border p-3 text-left transition-colors ${
                activeDevice === d.id ? 'border-primary bg-primary/5' : 'border-border hover:border-border-strong'
              }`}
            >
              <div className="flex items-center gap-2">
                <Server className="h-4 w-4 text-muted" />
                <span className="truncate text-sm font-medium">{d.hostname}</span>
              </div>
              <div className="mt-2"><CapacityBar used={d.used} total={d.total} /></div>
              <div className="mt-1.5 flex items-center gap-2 text-[11px] text-muted">
                <span>{d.free} free</span>·<span>{d.uplinks} uplinks</span>·<span>{d.active} active</span>
              </div>
            </button>
          ))
        )}
      </div>

      {/* Port table */}
      <Card>
        <CardContent className="p-0">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h3 className="text-sm font-semibold">
              {ports.data?.device.hostname || 'Ports'}
              {ports.data && <span className="ml-2 text-xs font-normal text-muted">{ports.data.ports.length} ports</span>}
            </h3>
            {activeDevice && (
              <Button size="sm" variant="ghost" onClick={() => navigate(`/devices/${activeDevice}`)}>Device page</Button>
            )}
          </div>
          {ports.isLoading ? (
            <div className="space-y-2 p-4">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
          ) : !ports.data || ports.data.ports.length === 0 ? (
            <div className="py-16 text-center text-sm text-muted">No ports to show.</div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <THead className="bg-surface2/40">
                  <Tr>
                    <Th>Port</Th><Th>Description</Th><Th>Status</Th><Th>Speed</Th>
                    <Th>VLAN</Th><Th className="text-right">Endpoints</Th><Th>Role</Th>
                    <Th className="text-right">Last device</Th><Th className="text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {ports.data.ports.map((p: UdtPort) => {
                    const up = p.oper_status === 'up'
                    const adminDown = p.admin_status === 'down'
                    return (
                      <Tr key={p.if_index}>
                        <Td className="font-medium">{p.if_name || `if ${p.if_index}`}</Td>
                        <Td className="max-w-[220px] truncate text-xs text-muted" title={p.if_alias || p.if_descr || ''}>
                          {p.if_alias || p.if_descr || '—'}
                        </Td>
                        <Td>
                          {adminDown ? <Badge variant="outline">admin down</Badge>
                            : up ? <Badge variant="success">up</Badge>
                            : <Badge variant="danger">down</Badge>}
                        </Td>
                        <Td className="text-xs tabular-nums text-muted">{speedLabel(p.if_speed)}</Td>
                        <Td className="text-xs tabular-nums">
                          {p.vlan_ids && p.vlan_ids.length ? p.vlan_ids.join(', ') : (p.pvid ?? '—')}
                        </Td>
                        <Td className="text-right tabular-nums">
                          {p.active_endpoints > 0
                            ? <button className="text-primary hover:underline"
                                onClick={() => navigate(`/udt?device=${activeDevice}`)}>{p.active_endpoints}</button>
                            : <span className="text-muted">0</span>}
                        </Td>
                        <Td>
                          {p.is_uplink ? (
                            <span className="inline-flex items-center gap-1">
                              <Badge variant="info"><ArrowUpFromLine className="mr-0.5 inline h-3 w-3" />uplink</Badge>
                              {p.uplink_reason && <span className="text-[10px] text-muted">{p.uplink_reason}</span>}
                            </span>
                          ) : <span className="text-xs text-muted">access</span>}
                        </Td>
                        <Td className="text-right text-xs text-muted">{relTime(p.last_endpoint_seen)}</Td>
                        <Td className="text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              title={p.is_uplink ? 'Mark as access port' : 'Mark as uplink'}
                              onClick={() => overrideMut.mutate({ port: p, value: p.is_uplink ? 'access' : 'uplink' })}
                              className="rounded p-1 text-muted hover:bg-surface2 hover:text-text"
                            >
                              <ArrowUpFromLine className="h-4 w-4" />
                            </button>
                            {up ? (
                              <button title="Shut down port" onClick={() => setConfirm({ port: p, action: 'shutdown' })}
                                className="rounded p-1 text-danger hover:bg-danger/10">
                                <PowerOff className="h-4 w-4" />
                              </button>
                            ) : (
                              <button title="Enable port" onClick={() => setConfirm({ port: p, action: 'enable' })}
                                className="rounded p-1 text-success hover:bg-success/10">
                                <Power className="h-4 w-4" />
                              </button>
                            )}
                          </div>
                        </Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={!!confirm}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm?.action === 'shutdown' ? 'Shut down port?' : 'Enable port?'}
        description={confirm
          ? `This sends an SNMP SET to ${ports.data?.device.hostname} to ${confirm.action === 'shutdown' ? 'administratively disable' : 'enable'} ${confirm.port.if_name || `if ${confirm.port.if_index}`}. Requires a write-capable SNMP credential on the switch.`
          : ''}
        confirmText={confirm?.action === 'shutdown' ? 'Shut down' : 'Enable'}
        destructive={confirm?.action === 'shutdown'}
        loading={action.isPending}
        onConfirm={() => {
          if (confirm) action.mutate({ port: confirm.port, act: confirm.action }, { onSettled: () => setConfirm(null) })
        }}
      />
    </div>
  )
}
