import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { KeyRound, Network, PlugZap, Plus, RefreshCw, ServerCog, Trash2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { udtApi } from './api'
import type { DomainController, UdtDeviceSettings } from './types'
import { ENDPOINT_TYPE_META, relTime } from './helpers'

const DEVICE_CRED = '__device__' // sentinel: use the device's own SNMP settings

function statusBadge(s: string | null) {
  if (s === 'ok') return <Badge variant="success">ok</Badge>
  if (s === 'error') return <Badge variant="danger">error</Badge>
  return <Badge variant="outline">never polled</Badge>
}

function AddDCDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [credId, setCredId] = useState('')
  const [interval, setInterval] = useState(300)

  const creds = useQuery({ queryKey: ['windows-credentials'], queryFn: async () => (await api.get('/windows-credentials')).data })

  const create = useMutation({
    mutationFn: () => udtApi.createDC({ name, host, windows_credential_id: credId, poll_interval_s: interval }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['udt', 'dcs'] }); toast.success('Domain controller added'); onClose() },
    onError: (e: any) => toast.error('Could not add', apiErrorMessage(e)),
  })

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add domain controller</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2">
          <div><label className="mb-1 block text-xs text-muted">Name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="DC01" /></div>
          <div><label className="mb-1 block text-xs text-muted">Host / IP (WinRM)</label>
            <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="dc01.corp.local" /></div>
          <div>
            <label className="mb-1 block text-xs text-muted">Windows credential</label>
            <Select value={credId} onValueChange={setCredId}>
              <SelectTrigger><SelectValue placeholder="Select credential…" /></SelectTrigger>
              <SelectContent>
                {(creds.data || []).map((c: any) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
              </SelectContent>
            </Select>
            {(creds.data || []).length === 0 && (
              <p className="mt-1 text-[11px] text-muted">
                No Windows credentials yet — add one under <Link to="/credentials?tab=windows" className="text-primary hover:underline">Credentials</Link>.
              </p>
            )}
          </div>
          <div><label className="mb-1 block text-xs text-muted">Poll interval (seconds)</label>
            <Input type="number" value={interval} onChange={(e) => setInterval(Number(e.target.value))} min={60} /></div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!name || !host || !credId || create.isPending} onClick={() => create.mutate()}>Add</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RowIntervalInput({ row, globalInterval, onSave }: {
  row: UdtDeviceSettings
  globalInterval: number
  onSave: (v: number | null) => void
}) {
  const [val, setVal] = useState(row.poll_interval_s == null ? '' : String(row.poll_interval_s))
  useEffect(() => { setVal(row.poll_interval_s == null ? '' : String(row.poll_interval_s)) }, [row.poll_interval_s])
  const commit = () => {
    const next = val.trim() === '' ? null : Math.max(60, Math.min(86400, Number(val)))
    if (next !== row.poll_interval_s) onSave(next)
    if (next != null) setVal(String(next))
  }
  return (
    <Input
      type="number" min={60} max={86400}
      className="h-7 w-24 text-xs"
      placeholder={`${globalInterval}s`}
      value={val}
      onChange={(e) => setVal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
    />
  )
}

function DevicePolling() {
  const qc = useQueryClient()
  const settings = useQuery({ queryKey: ['udt', 'settings'], queryFn: () => udtApi.settings() })
  const deviceSettings = useQuery({
    queryKey: ['udt', 'device-settings'],
    queryFn: () => udtApi.deviceSettings(),
    refetchInterval: 30_000,
  })
  const [globalInterval, setGlobalInterval] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [showAll, setShowAll] = useState(false)
  useEffect(() => {
    if (settings.data) setGlobalInterval(String(settings.data.poll_interval_s))
  }, [settings.data])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['udt', 'device-settings'] })
    qc.invalidateQueries({ queryKey: ['udt', 'settings'] })
  }
  const saveGlobal = useMutation({
    mutationFn: () => udtApi.updateSettings({ poll_interval_s: Math.max(30, Math.min(86400, Number(globalInterval))) }),
    onSuccess: () => { invalidate(); toast.success('Global UDT poll interval saved') },
    onError: (e: any) => toast.error('Could not save', apiErrorMessage(e)),
  })
  const saveDevice = useMutation({
    mutationFn: ({ row, patch }: { row: UdtDeviceSettings; patch: Partial<{ enabled: boolean; snmp_credential_id: string | null; poll_interval_s: number | null }> }) =>
      udtApi.updateDeviceSettings(row.device_id, {
        enabled: row.enabled,
        snmp_credential_id: row.snmp_credential_id,
        poll_interval_s: row.poll_interval_s,
        ...patch,
      }),
    onSuccess: () => invalidate(),
    onError: (e: any) => { toast.error('Could not save', apiErrorMessage(e)); invalidate() },
  })
  const bulk = useMutation({
    mutationFn: (body: Record<string, any>) => udtApi.bulkDeviceSettings({ device_ids: Array.from(selected), ...body }),
    onSuccess: (r: any) => { invalidate(); setSelected(new Set()); toast.success(`Updated ${r.updated ?? 0} devices`) },
    onError: (e: any) => toast.error('Bulk update failed', apiErrorMessage(e)),
  })

  const allRows = deviceSettings.data?.data || []
  const rows = showAll ? allRows : allRows.filter((r) => r.is_l2)
  const hiddenCount = allRows.length - allRows.filter((r) => r.is_l2).length
  const creds = deviceSettings.data?.credentials || []
  const allSelected = rows.length > 0 && selected.size === rows.length
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(rows.map((r) => r.device_id)))
  const toggleOne = (id: string) => setSelected((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold"><Network className="h-4 w-4 text-primary" /> Layer-2 polling</h3>
            <p className="mt-0.5 text-xs text-muted">
              Layer-2 devices (switches and anything returning bridge-table data) that UDT polls for MAC/ARP/LLDP tables.
              Devices are enabled by default and use their own SNMP settings unless overridden here.
              Per-port exclusions live on the <Link to="/udt/ports" className="text-primary hover:underline">Switch Ports</Link> tab.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {hiddenCount > 0 && (
              <label className="mr-2 flex cursor-pointer items-center gap-1.5 text-xs text-muted">
                <Switch checked={showAll} onCheckedChange={(v) => { setShowAll(v); setSelected(new Set()) }} />
                Show all SNMP devices ({hiddenCount} non-L2)
              </label>
            )}
            <label className="text-xs text-muted">Default interval</label>
            <Input
              type="number" min={30} max={86400}
              className="h-8 w-24 text-xs"
              value={globalInterval}
              onChange={(e) => setGlobalInterval(e.target.value)}
            />
            <span className="text-xs text-muted">s</span>
            <Button size="sm" variant="outline"
              disabled={saveGlobal.isPending || !globalInterval || Number(globalInterval) === settings.data?.poll_interval_s}
              onClick={() => saveGlobal.mutate()}>
              Save
            </Button>
          </div>
        </div>

        {selected.size > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-border bg-primary/5 px-4 py-2">
            <span className="text-xs font-medium">{selected.size} selected</span>
            <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => bulk.mutate({ set_enabled: true })}>Enable UDT</Button>
            <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => bulk.mutate({ set_enabled: false })}>Disable UDT</Button>
            <div className="flex items-center gap-1">
              <KeyRound className="h-3.5 w-3.5 text-muted" />
              <Select value="" onValueChange={(v) => bulk.mutate({ set_credential: true, snmp_credential_id: v === DEVICE_CRED ? null : v })}>
                <SelectTrigger className="h-8 w-52 text-xs"><SelectValue placeholder="Assign credential…" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEVICE_CRED}>Device SNMP settings</SelectItem>
                  {creds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} (v{c.snmp_version})</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
          </div>
        )}

        {deviceSettings.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
        ) : rows.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted">
            No layer-2 devices found — enable SNMP on your switches{hiddenCount > 0 ? ', or use "Show all SNMP devices" above' : ''}.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <THead className="bg-surface2/40">
                <Tr>
                  <Th className="w-8"><input type="checkbox" className="accent-primary" checked={allSelected} onChange={toggleAll} /></Th>
                  <Th>Device</Th><Th>UDT credential</Th><Th>Interval</Th>
                  <Th className="text-right">Ports monitored</Th><Th className="text-right">Endpoints</Th>
                  <Th>Last poll</Th><Th>UDT</Th>
                </Tr>
              </THead>
              <TBody>
                {rows.map((r) => (
                  <Tr key={r.device_id} className={r.enabled ? '' : 'opacity-60'}>
                    <Td><input type="checkbox" className="accent-primary" checked={selected.has(r.device_id)} onChange={() => toggleOne(r.device_id)} /></Td>
                    <Td>
                      <div className="flex items-center gap-1.5">
                        <Link to={`/devices/${r.device_id}`} className="font-medium hover:text-primary">{r.hostname}</Link>
                        {!r.is_l2 && <Badge variant="outline">{r.device_type}</Badge>}
                      </div>
                      <div className="font-mono text-[11px] text-muted">{r.ip || '—'}{r.vendor ? ` · ${r.vendor}` : ''}</div>
                    </Td>
                    <Td>
                      <Select
                        value={r.snmp_credential_id || DEVICE_CRED}
                        onValueChange={(v) => saveDevice.mutate({ row: r, patch: { snmp_credential_id: v === DEVICE_CRED ? null : v } })}
                      >
                        <SelectTrigger className="h-7 w-48 text-xs"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value={DEVICE_CRED}>Device SNMP settings</SelectItem>
                          {creds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} (v{c.snmp_version})</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </Td>
                    <Td>
                      <RowIntervalInput
                        row={r}
                        globalInterval={settings.data?.poll_interval_s ?? 300}
                        onSave={(v) => saveDevice.mutate({ row: r, patch: { poll_interval_s: v } })}
                      />
                    </Td>
                    <Td className="text-right text-xs tabular-nums">
                      {r.ports_total > 0
                        ? <span className={r.ports_monitored < r.ports_total ? 'text-warning' : ''}>{r.ports_monitored}/{r.ports_total}</span>
                        : <span className="text-muted">—</span>}
                    </Td>
                    <Td className="text-right text-xs tabular-nums">{r.active_endpoints}</Td>
                    <Td className="text-xs text-muted">{relTime(r.last_udt_at)}</Td>
                    <Td><Switch checked={r.enabled} onCheckedChange={(v) => saveDevice.mutate({ row: r, patch: { enabled: v } })} /></Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function DomainControllers() {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const dcs = useQuery({ queryKey: ['udt', 'dcs'], queryFn: () => udtApi.domainControllers(), refetchInterval: 30_000 })

  const poll = useMutation({
    mutationFn: (id: string) => udtApi.pollDC(id),
    onSuccess: (r: any) => {
      qc.invalidateQueries({ queryKey: ['udt', 'dcs'] })
      if (r.status === 'ok') toast.success(`Polled: ${r.inserted ?? 0} new logins from ${r.events ?? 0} events`)
      else toast.error('Poll failed', r.error || 'unknown')
    },
    onError: (e: any) => toast.error('Poll failed', apiErrorMessage(e)),
  })
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => udtApi.updateDC(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['udt', 'dcs'] }),
  })
  const del = useMutation({
    mutationFn: (id: string) => udtApi.deleteDC(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['udt', 'dcs'] }); toast.success('Removed') },
  })

  const data = dcs.data?.data || []

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold"><ServerCog className="h-4 w-4 text-primary" /> Active Directory controllers</h3>
            <p className="mt-0.5 text-xs text-muted">
              Agentless user-login correlation. ZenPlus reads each DC's Security event log over WinRM (events 4624 / 4768 / 4769)
              and maps logons to endpoints.
            </p>
          </div>
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}><Plus className="mr-1 h-4 w-4" />Add DC</Button>
        </div>
        {dcs.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 2 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
        ) : data.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted">No domain controllers configured.</div>
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr><Th>Name</Th><Th>Host</Th><Th>Credential</Th><Th>Status</Th><Th>Last poll</Th><Th>Enabled</Th><Th className="text-right">Actions</Th></Tr>
            </THead>
            <TBody>
              {data.map((dc: DomainController) => (
                <Tr key={dc.id}>
                  <Td className="font-medium">{dc.name}</Td>
                  <Td className="font-mono text-xs">{dc.host}</Td>
                  <Td className="text-xs text-muted">{dc.credential_name || '—'}</Td>
                  <Td>
                    <div className="flex items-center gap-1">
                      {statusBadge(dc.last_status)}
                      {dc.last_error && <span className="max-w-[140px] truncate text-[10px] text-danger" title={dc.last_error}>{dc.last_error}</span>}
                    </div>
                  </Td>
                  <Td className="text-xs text-muted">{relTime(dc.last_poll_at)}</Td>
                  <Td><Switch checked={dc.enabled} onCheckedChange={(v) => toggle.mutate({ id: dc.id, enabled: v })} /></Td>
                  <Td className="text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button title="Poll now" disabled={poll.isPending} className="rounded p-1 text-primary hover:bg-primary/10"
                        onClick={() => poll.mutate(dc.id)}>
                        <RefreshCw className={`h-4 w-4 ${poll.isPending ? 'animate-spin' : ''}`} />
                      </button>
                      <button title="Delete" className="rounded p-1 text-danger hover:bg-danger/10" onClick={() => del.mutate(dc.id)}>
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
      {adding && <AddDCDialog onClose={() => setAdding(false)} />}
    </Card>
  )
}

function VendorBreakdown() {
  const vendors = useQuery({ queryKey: ['udt', 'vendors'], queryFn: () => udtApi.vendors(), refetchInterval: 60_000 })
  const data = (vendors.data?.data || []).slice(0, 15)
  const max = Math.max(1, ...data.map((v) => v.count))
  return (
    <Card>
      <CardContent className="p-0">
        <h3 className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold">
          <PlugZap className="h-4 w-4 text-primary" /> Endpoints by vendor (OUI)
        </h3>
        {vendors.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-6 w-full" />)}</div>
        ) : data.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted">No endpoints yet.</div>
        ) : (
          <ul className="divide-y divide-border">
            {data.map((v) => (
              <li key={v.vendor} className="flex items-center gap-3 px-4 py-2">
                <div className="w-44 truncate text-xs" title={v.vendor}>{v.vendor}</div>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface3">
                  <div className="h-full bg-primary/70" style={{ width: `${(v.count / max) * 100}%` }} />
                </div>
                <div className="w-8 text-right text-xs tabular-nums text-muted">{v.count}</div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

export function SettingsPage() {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="lg:col-span-2"><DevicePolling /></div>
      <div className="lg:col-span-2"><DomainControllers /></div>
      <VendorBreakdown />
      <Card>
        <CardContent className="space-y-3 p-4 text-xs text-muted">
          <h3 className="text-sm font-semibold text-text">How UDT collects data</h3>
          <p>Each SNMP-enabled switch is polled (every 5 minutes by default — configurable above, globally or per device) for its
            bridge forwarding database (BRIDGE-MIB / Q-BRIDGE-MIB), ARP/neighbor tables (IP-MIB) and LLDP/CDP neighbors.
            MAC-to-port, IP-to-MAC and user-to-endpoint are correlated into unified endpoints. Ports excluded from monitoring
            keep their capacity stats but produce no endpoints or events.</p>
          <p>Uplink and trunk ports are detected automatically from LLDP/CDP neighbors, Cisco VTP trunk status and MAC-count
            heuristics, so aggregation ports don't flood the endpoint views. Override any port's role on the Switch Ports tab.</p>
          <p>Endpoint types below are inferred from OUI vendor and hostname:</p>
          <div className="flex flex-wrap gap-1.5">
            {Object.values(ENDPOINT_TYPE_META).map((m) => (
              <span key={m.label} className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5">
                <m.icon className="h-3 w-3" /> {m.label}
              </span>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
