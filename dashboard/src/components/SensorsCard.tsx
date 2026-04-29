/**
 * SensorsCard — admin UI for remote sensors (Phase 1).
 *
 * Embedded on /settings/general. Provides:
 *   - List of sensors with status, last heartbeat, version
 *   - "Add Sensor" dialog → returns a one-time enrollment token + install command
 *   - Detail dialog with assignments, key rotation, disable, delete
 *   - Auto-refresh every 5s so newly-enrolled sensors appear without reload
 */
import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, Copy, Loader2, Pause, Plus, RotateCw, Trash2, Plug, Pencil, Check, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'

type Sensor = {
  id: string
  name: string
  description: string | null
  site_id: string | null
  site_name: string | null
  location: string | null
  status: 'pending' | 'online' | 'degraded' | 'offline' | 'disabled'
  version: string | null
  last_seen_at: string | null
  last_heartbeat_at: string | null
  last_ip: string | null
  queue_depth: number
  queue_dropped_count: number
  hostname: string | null
  os_info: string | null
  uptime_seconds: number | null
  api_key_prefix: string | null
  enrollment_pending: boolean
  enrollment_expires_at: string | null
  assignment_count: number
  tags: string[]
  created_at: string
  updated_at: string
}

type TokenInfo = {
  sensor_id: string
  enrollment_token: string
  expires_at: string
  server_url: string
  install_command: string
}

type Site = { id: string; name: string; region: string | null; sensor_count: number }
type Device = { id: string; hostname: string; ip_address: string }
type ServiceCheck = { id: string; name: string; check_type: string }
type Assignment = {
  sensor_id: string
  target_type: 'device' | 'service_check' | 'group'
  target_id: string
  target_name: string | null
  priority: number
  created_at: string
}

const statusVariant: Record<Sensor['status'], 'success' | 'warning' | 'danger' | 'outline' | 'info'> = {
  online: 'success',
  pending: 'info',
  degraded: 'warning',
  offline: 'danger',
  disabled: 'outline',
}


function copyToClipboard(s: string, label: string) {
  if (typeof navigator === 'undefined' || !navigator.clipboard) {
    toast.info('Copy manually', s)
    return
  }
  navigator.clipboard.writeText(s).then(
    () => toast.success(`${label} copied`),
    () => toast.error('Copy failed'),
  )
}


export function SensorsCard() {
  const qc = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [tokenInfo, setTokenInfo] = useState<TokenInfo | null>(null)
  const [detail, setDetail] = useState<Sensor | null>(null)
  const [deleting, setDeleting] = useState<Sensor | null>(null)

  const { data: sensors, isLoading } = useQuery<Sensor[]>({
    queryKey: ['sensors'],
    queryFn: async () => (await api.get('/sensors')).data,
    refetchInterval: 5_000,
  })

  const { data: sites } = useQuery<Site[]>({
    queryKey: ['sites'],
    queryFn: async () => (await api.get('/sites')).data,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/sensors/${id}`),
    onSuccess: () => {
      toast.success('Sensor deleted')
      qc.invalidateQueries({ queryKey: ['sensors'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Plug className="h-4 w-4 text-primary" />
            Remote Sensors
          </CardTitle>
          <p className="mt-1 text-xs text-muted">
            Distributed pollers at branch sites. Each sensor enrolls once with a one-time token and then
            heartbeats over HTTPS. Allow outbound 443 from the sensor to this server.
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4" /> Add Sensor
        </Button>
      </CardHeader>
      <CardContent>
        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th>Site</Th>
              <Th>Location</Th>
              <Th>Last heartbeat</Th>
              <Th>Version</Th>
              <Th>Assignments</Th>
              <Th className="w-32 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {isLoading && (
              <Tr>
                <Td colSpan={8} className="py-6 text-center text-muted">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </Td>
              </Tr>
            )}
            {!isLoading && (sensors || []).length === 0 && (
              <Tr>
                <Td colSpan={8} className="py-8 text-center text-muted">
                  No sensors yet — click <span className="font-medium text-text">Add Sensor</span> to register the first one.
                </Td>
              </Tr>
            )}
            {(sensors || []).map((s) => (
              <Tr key={s.id} className="hover:bg-surface2/40">
                <Td>
                  <button
                    className="font-medium text-text hover:text-primary"
                    onClick={() => setDetail(s)}
                  >
                    {s.name}
                  </button>
                  {s.enrollment_pending && (
                    <div className="mt-0.5 text-xs text-warning">
                      Enrollment pending — token shown to operator
                    </div>
                  )}
                  {s.description && (
                    <div className="mt-0.5 text-xs text-muted">{s.description}</div>
                  )}
                </Td>
                <Td>
                  <Badge variant={statusVariant[s.status]}>{s.status}</Badge>
                </Td>
                <Td className="text-sm">{s.site_name || '—'}</Td>
                <Td className="text-sm">{s.location || '—'}</Td>
                <Td className="text-xs text-muted">
                  {relativeTime(s.last_heartbeat_at)}
                </Td>
                <Td className="text-xs">{s.version || '—'}</Td>
                <Td className="text-xs">{s.assignment_count}</Td>
                <Td>
                  <div className="flex justify-end gap-1">
                    <Button size="sm" variant="ghost" onClick={() => setDetail(s)}>
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-muted hover:text-danger"
                      onClick={() => setDeleting(s)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </CardContent>

      <AddSensorDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        sites={sites || []}
        onCreated={(token) => {
          setTokenInfo(token)
          qc.invalidateQueries({ queryKey: ['sensors'] })
        }}
      />

      <TokenDialog
        info={tokenInfo}
        onClose={() => setTokenInfo(null)}
      />

      <SensorDetailDialog
        sensor={detail}
        onClose={() => setDetail(null)}
        onChanged={() => qc.invalidateQueries({ queryKey: ['sensors'] })}
        onShowToken={(t) => setTokenInfo(t)}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete sensor"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>?
            This removes its API key and all assignments — historical metrics are kept.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </Card>
  )
}

/* ───────── Add Sensor dialog ───────── */

function AddSensorDialog({
  open, onOpenChange, sites, onCreated,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  sites: Site[]
  onCreated: (token: TokenInfo) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [siteId, setSiteId] = useState<string>('')
  const [location, setLocation] = useState('')

  useEffect(() => {
    if (!open) {
      setName(''); setDescription(''); setSiteId(''); setLocation('')
    }
  }, [open])

  const create = useMutation({
    mutationFn: async () => {
      const payload: any = { name, description: description || null, location: location || null }
      if (siteId) payload.site_id = siteId
      return (await api.post('/sensors', payload)).data
    },
    onSuccess: (res) => {
      onOpenChange(false)
      onCreated(res.token)
    },
    onError: (e: any) => toast.error('Create failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add Sensor</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate() }}
        >
          <FormField label="Name" required hint="e.g. branch-karachi-01">
            <Input required value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </FormField>
          <FormField label="Site">
            <Select value={siteId} onValueChange={setSiteId}>
              <SelectTrigger><SelectValue placeholder="(none)" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="">(none)</SelectItem>
                {sites.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}{s.region ? ` — ${s.region}` : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Location" hint="Free-text, e.g. Karachi DC, Rack 12">
            <Input value={location} onChange={(e) => setLocation(e.target.value)} />
          </FormField>
          <FormField label="Description">
            <Textarea value={description} onChange={(e) => setDescription(e.target.value)} />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !name}>
              {create.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Create &amp; issue token
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/* ───────── Token dialog (shown ONCE) ───────── */

function TokenDialog({ info, onClose }: { info: TokenInfo | null; onClose: () => void }) {
  return (
    <Dialog open={!!info} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-success" />
            Sensor enrollment token
          </DialogTitle>
        </DialogHeader>
        {info && (
          <div className="space-y-4">
            <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning">
              <strong>Token shown only once.</strong> Copy the install command below and run it on the sensor VM
              before <span className="font-medium">{new Date(info.expires_at).toLocaleString()}</span>.
              You can re-issue a fresh token from the sensor's detail dialog if needed.
            </div>

            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
                Server URL
              </div>
              <div className="flex items-center gap-2 rounded-md border border-border bg-surface2 px-3 py-2 font-mono text-xs">
                <span className="flex-1 break-all">{info.server_url}</span>
                <Button size="sm" variant="ghost" onClick={() => copyToClipboard(info.server_url, 'URL')}>
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
                Enrollment token
              </div>
              <div className="flex items-center gap-2 rounded-md border border-border bg-surface2 px-3 py-2 font-mono text-xs">
                <span className="flex-1 break-all">{info.enrollment_token}</span>
                <Button size="sm" variant="ghost" onClick={() => copyToClipboard(info.enrollment_token, 'Token')}>
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
                One-line install command (Phase 1: mock sensor)
              </div>
              <div className="rounded-md border border-border bg-surface2 p-3 font-mono text-xs leading-relaxed">
                <pre className="whitespace-pre-wrap break-all">{info.install_command}</pre>
              </div>
              <div className="mt-2 flex justify-end">
                <Button size="sm" variant="outline" onClick={() => copyToClipboard(info.install_command, 'Command')}>
                  <Copy className="h-3.5 w-3.5" /> Copy command
                </Button>
              </div>
            </div>
          </div>
        )}
        <DialogFooter>
          <Button onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ───────── Sensor detail dialog ───────── */

function SensorDetailDialog({
  sensor, onClose, onChanged, onShowToken,
}: {
  sensor: Sensor | null
  onClose: () => void
  onChanged: () => void
  onShowToken: (t: TokenInfo) => void
}) {
  const open = !!sensor
  const qc = useQueryClient()

  const { data: assignments } = useQuery<Assignment[]>({
    queryKey: ['sensor-assignments', sensor?.id],
    enabled: !!sensor?.id,
    queryFn: async () => (await api.get(`/sensors/${sensor!.id}/assignments`)).data,
  })

  const { data: devices } = useQuery<Device[]>({
    queryKey: ['devices-light'],
    enabled: open,
    queryFn: async () => {
      const r = (await api.get('/devices?limit=200')).data
      const arr = r?.devices || r?.data || (Array.isArray(r) ? r : [])
      return arr.map((d: any) => ({ id: d.id, hostname: d.hostname, ip_address: d.ip_address }))
    },
  })

  const { data: serviceChecks } = useQuery<ServiceCheck[]>({
    queryKey: ['service-checks-light'],
    enabled: open,
    queryFn: async () => {
      const r = (await api.get('/service-checks?limit=500')).data
      const arr = r?.service_checks || r?.data || (Array.isArray(r) ? r : [])
      return arr.map((s: any) => ({ id: s.id, name: s.name, check_type: s.check_type }))
    },
  })

  const regen = useMutation({
    mutationFn: async () => (await api.post(`/sensors/${sensor!.id}/regenerate-token`)).data,
    onSuccess: (t) => { onShowToken(t); onChanged() },
    onError: (e: any) => toast.error('Token regen failed', apiErrorMessage(e)),
  })

  const rotate = useMutation({
    mutationFn: async () => (await api.post(`/sensors/${sensor!.id}/rotate-key`)).data,
    onSuccess: (r) => {
      toast.success('API key rotated')
      copyToClipboard(r.api_key, 'New API key')
    },
    onError: (e: any) => toast.error('Rotate failed', apiErrorMessage(e)),
  })

  const disable = useMutation({
    mutationFn: async () => (await api.post(`/sensors/${sensor!.id}/disable`)).data,
    onSuccess: () => { toast.success('Sensor disabled'); onChanged() },
    onError: (e: any) => toast.error('Disable failed', apiErrorMessage(e)),
  })

  const enable = useMutation({
    mutationFn: async () => (await api.post(`/sensors/${sensor!.id}/enable`)).data,
    onSuccess: () => { toast.success('Sensor enabled'); onChanged() },
    onError: (e: any) => toast.error('Enable failed', apiErrorMessage(e)),
  })

  const replaceAssignments = useMutation({
    mutationFn: async (items: { target_type: string; target_id: string; priority: number }[]) =>
      (await api.put(`/sensors/${sensor!.id}/assignments`, { items })).data,
    onSuccess: () => {
      toast.success('Assignments updated')
      qc.invalidateQueries({ queryKey: ['sensor-assignments', sensor!.id] })
      onChanged()
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  // Local editable state for assignments
  const [editing, setEditing] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  useEffect(() => {
    if (!editing && assignments) {
      setPicked(new Set(assignments.map((a) => `${a.target_type}:${a.target_id}`)))
    }
  }, [editing, assignments])

  const togglePick = (key: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const saveAssignments = () => {
    const items = Array.from(picked).map((k) => {
      const [target_type, target_id] = k.split(':')
      return { target_type, target_id, priority: 100 }
    })
    replaceAssignments.mutate(items)
    setEditing(false)
  }

  if (!sensor) return null

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{sensor.name}</DialogTitle>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3 text-sm">
          <Row k="Status" v={<Badge variant={statusVariant[sensor.status]}>{sensor.status}</Badge>} />
          <Row k="Site" v={sensor.site_name || '—'} />
          <Row k="Location" v={sensor.location || '—'} />
          <Row k="Last heartbeat" v={relativeTime(sensor.last_heartbeat_at)} />
          <Row k="Version" v={sensor.version || '—'} />
          <Row k="Hostname" v={sensor.hostname || '—'} />
          <Row k="Last IP" v={sensor.last_ip || '—'} />
          <Row k="Queue depth" v={String(sensor.queue_depth)} />
          <Row k="API key" v={sensor.api_key_prefix ? `${sensor.api_key_prefix}…` : '— (not enrolled)'} />
          <Row k="Created" v={new Date(sensor.created_at).toLocaleString()} />
        </div>

        <div className="my-4 border-t border-border" />

        <div>
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-medium">Assignments ({assignments?.length || 0})</div>
            {!editing ? (
              <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                <Pencil className="h-3.5 w-3.5" /> Edit
              </Button>
            ) : (
              <div className="flex gap-1">
                <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
                  <X className="h-3.5 w-3.5" /> Cancel
                </Button>
                <Button size="sm" onClick={saveAssignments} disabled={replaceAssignments.isPending}>
                  <Check className="h-3.5 w-3.5" /> Save
                </Button>
              </div>
            )}
          </div>

          {!editing ? (
            <div className="rounded-md border border-border">
              {(assignments || []).length === 0 ? (
                <div className="px-3 py-6 text-center text-xs text-muted">No assignments yet</div>
              ) : (
                <ul className="divide-y divide-border">
                  {(assignments || []).map((a) => (
                    <li key={`${a.target_type}:${a.target_id}`} className="flex items-center justify-between px-3 py-2 text-sm">
                      <span>
                        <Badge variant="outline" className="mr-2">{a.target_type}</Badge>
                        {a.target_name || a.target_id}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <AssignmentPicker
              devices={devices || []}
              serviceChecks={serviceChecks || []}
              picked={picked}
              onToggle={togglePick}
            />
          )}
        </div>

        <DialogFooter className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => regen.mutate()} disabled={regen.isPending}>
            <RotateCw className="h-3.5 w-3.5" /> Regenerate enrollment token
          </Button>
          <Button variant="outline" onClick={() => rotate.mutate()} disabled={rotate.isPending}>
            <Activity className="h-3.5 w-3.5" /> Rotate API key
          </Button>
          {sensor.status === 'disabled' ? (
            <Button variant="outline" onClick={() => enable.mutate()}>Enable</Button>
          ) : (
            <Button variant="outline" onClick={() => disable.mutate()}>
              <Pause className="h-3.5 w-3.5" /> Disable
            </Button>
          )}
          <div className="flex-1" />
          <Button onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function AssignmentPicker({
  devices, serviceChecks, picked, onToggle,
}: {
  devices: Device[]
  serviceChecks: ServiceCheck[]
  picked: Set<string>
  onToggle: (key: string) => void
}) {
  const [filter, setFilter] = useState('')
  const f = filter.toLowerCase().trim()

  const filteredDevices = useMemo(
    () => (f ? devices.filter((d) => d.hostname.toLowerCase().includes(f) || d.ip_address.includes(f)) : devices),
    [devices, f],
  )
  const filteredChecks = useMemo(
    () => (f ? serviceChecks.filter((s) => s.name.toLowerCase().includes(f)) : serviceChecks),
    [serviceChecks, f],
  )

  return (
    <div className="rounded-md border border-border">
      <div className="border-b border-border p-2">
        <Input placeholder="Filter devices and checks…" value={filter} onChange={(e) => setFilter(e.target.value)} />
      </div>
      <div className="max-h-72 overflow-y-auto p-2">
        {filteredDevices.length > 0 && (
          <>
            <div className="px-1 pb-1 text-xs font-medium uppercase tracking-wider text-muted">Devices</div>
            {filteredDevices.map((d) => {
              const key = `device:${d.id}`
              return (
                <label key={key} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-surface2">
                  <input type="checkbox" checked={picked.has(key)} onChange={() => onToggle(key)} />
                  <span className="text-sm">{d.hostname}</span>
                  <span className="text-xs text-muted">{d.ip_address}</span>
                </label>
              )
            })}
          </>
        )}
        {filteredChecks.length > 0 && (
          <>
            <div className="mt-2 px-1 pb-1 text-xs font-medium uppercase tracking-wider text-muted">Service Checks</div>
            {filteredChecks.map((s) => {
              const key = `service_check:${s.id}`
              return (
                <label key={key} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-surface2">
                  <input type="checkbox" checked={picked.has(key)} onChange={() => onToggle(key)} />
                  <span className="text-sm">{s.name}</span>
                  <Badge variant="outline" className="ml-auto">{s.check_type}</Badge>
                </label>
              )
            })}
          </>
        )}
        {filteredDevices.length === 0 && filteredChecks.length === 0 && (
          <div className="py-4 text-center text-xs text-muted">No matches</div>
        )}
      </div>
    </div>
  )
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
      <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
      <span className="text-right">{v}</span>
    </div>
  )
}
