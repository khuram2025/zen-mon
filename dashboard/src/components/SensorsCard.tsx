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
  Activity, Copy, Download, Loader2, Pause, Plus, RotateCw, Trash2, Plug, Pencil, Check, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, copyText, relativeTime } from '@/lib/utils'
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
import { Switch } from '@/components/ui/Switch'

type Sensor = {
  id: string
  name: string
  description: string | null
  site_id: string | null
  site_name: string | null
  location: string | null
  status: 'pending' | 'online' | 'degraded' | 'offline' | 'disabled'
  status_reason: string | null
  version: string | null
  last_seen_at: string | null
  last_heartbeat_at: string | null
  last_ip: string | null
  queue_depth: number
  queue_dropped_count: number
  heartbeat_interval_s: number
  degraded_after_s: number
  offline_after_s: number
  min_supported_version: string | null
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
  manifest_url?: string | null
  ova_url?: string | null
  ovf_url?: string | null
  bootstrap_cloud_init?: string | null
  bootstrap_meta_data?: string | null
  bootstrap_network_config?: string | null
  bootstrap_iso_url?: string | null
  configured_ova_url?: string | null
  bootstrap_warning?: string | null
}

type SensorDownloads = {
  sensor_id: string
  sensor_name: string
  manifest_url?: string | null
  ova_url?: string | null
  ovf_url?: string | null
  configured_ova_url?: string | null
  bootstrap_iso_url?: string | null
  configured_ova_size_bytes?: number | null
  bootstrap_iso_size_bytes?: number | null
  artifact_token?: string | null
  updated_at?: string | null
  note?: string | null
}

type Site = { id: string; name: string; region: string | null; sensor_count: number }
type Device = { id: string; hostname: string; ip_address: string }
type DeviceGroup = { id: string; name: string }
type ServiceCheck = { id: string; name: string; check_type: string }
type Assignment = {
  sensor_id: string
  target_type: 'device' | 'service_check' | 'group'
  target_id: string
  target_name: string | null
  priority: number
  created_at: string
}

type SensorEvent = {
  id: string
  sensor_id: string
  ts: string
  kind: string
  detail: Record<string, unknown>
}

type SensorCommand = {
  id: string
  sensor_id: string
  verb: 'update' | 'flush_buffer' | 'reload_config' | 'set_log_level'
  payload: Record<string, unknown>
  status: 'pending' | 'delivered' | 'succeeded' | 'failed' | 'expired'
  delivery_count: number
  last_delivered_at: string | null
  completed_at: string | null
  expires_at: string
  result: string | null
  created_at: string
}

type SensorVantage = {
  service_check_id: string
  service_check_name: string
  check_type: string
  state: string
  last_result_at: string
  last_latency_ms: number | null
  last_error: string | null
  tls_days_remaining: number | null
}

type ApplianceArtifact = {
  kind: 'ova' | 'ovf' | 'sha256'
  filename: string
  available: boolean
  url: string
  size_bytes: number | null
  updated_at: string | null
  sha256?: string | null
}

type ApplianceManifest = {
  product: string
  status?: 'preview' | 'ready' | 'not_published'
  note?: string
  metadata?: Record<string, unknown>
  artifact_dir: string
  artifacts: ApplianceArtifact[]
}

const statusVariant: Record<Sensor['status'], 'success' | 'warning' | 'danger' | 'outline' | 'info'> = {
  online: 'success',
  pending: 'info',
  degraded: 'warning',
  offline: 'danger',
  disabled: 'outline',
}

const NO_SITE_VALUE = '__none__'

function isVersionBehind(version: string | null, minimum: string | null) {
  if (!version || !minimum) return false
  const parts = (value: string) => (value.match(/\d+/g) || []).slice(0, 3).map(Number)
  const current = parts(version)
  const required = parts(minimum)
  for (let i = 0; i < Math.max(current.length, required.length); i += 1) {
    const delta = (current[i] || 0) - (required[i] || 0)
    if (delta !== 0) return delta < 0
  }
  return false
}


async function copyToClipboard(s: string, label: string) {
  if (await copyText(s)) toast.success(`${label} copied`)
  else toast.info('Copy manually', s)
}

function downloadText(filename: string, content: string) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}


export function SensorsCard() {
  const qc = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [tokenInfo, setTokenInfo] = useState<TokenInfo | null>(null)
  const [downloads, setDownloads] = useState<SensorDownloads | null>(null)
  const [detail, setDetail] = useState<Sensor | null>(null)
  const [deleting, setDeleting] = useState<Sensor | null>(null)
  const [siteFilter, setSiteFilter] = useState('all')

  const { data: sensors, isLoading } = useQuery<Sensor[]>({
    queryKey: ['sensors'],
    queryFn: async () => (await api.get('/sensors')).data,
    refetchInterval: 5_000,
  })

  useEffect(() => {
    if (!sensors) return
    setDetail((selected) => (
      selected ? sensors.find((sensor) => sensor.id === selected.id) || null : null
    ))
  }, [sensors])

  const { data: sites } = useQuery<Site[]>({
    queryKey: ['sites'],
    queryFn: async () => (await api.get('/sites')).data,
  })

  const { data: appliance, isLoading: applianceLoading } = useQuery<ApplianceManifest>({
    queryKey: ['sensor-appliance-manifest'],
    queryFn: async () => (await api.get('/sensor/appliance/manifest')).data,
    refetchInterval: 30_000,
  })

  const visibleSensors = useMemo(
    () => (sensors || []).filter((sensor) => (
      siteFilter === 'all'
      || (siteFilter === NO_SITE_VALUE ? !sensor.site_id : sensor.site_id === siteFilter)
    )),
    [sensors, siteFilter],
  )

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/sensors/${id}`),
    onSuccess: () => {
      toast.success('Sensor deleted')
      qc.invalidateQueries({ queryKey: ['sensors'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const loadDownloads = useMutation({
    mutationFn: async (sensor: Sensor) => (await api.get(`/sensors/${sensor.id}/downloads`)).data,
    onSuccess: (data: SensorDownloads) => setDownloads(data),
    onError: (e: any) => toast.error('Download lookup failed', apiErrorMessage(e)),
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
        <ApplianceDownloadPanel manifest={appliance} loading={applianceLoading} />

        <div className="mb-3 flex justify-end">
          <Select value={siteFilter} onValueChange={setSiteFilter}>
            <SelectTrigger className="w-56"><SelectValue placeholder="Filter by site" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All sites</SelectItem>
              <SelectItem value={NO_SITE_VALUE}>Unassigned site</SelectItem>
              {(sites || []).map((site) => <SelectItem key={site.id} value={site.id}>{site.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>

        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th>Site</Th>
              <Th>Location</Th>
              <Th>Last heartbeat</Th>
              <Th>Buffer</Th>
              <Th>Version</Th>
              <Th>Assignments</Th>
              <Th className="w-44 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {isLoading && (
              <Tr>
                <Td colSpan={9} className="py-6 text-center text-muted">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </Td>
              </Tr>
            )}
            {!isLoading && visibleSensors.length === 0 && (
              <Tr>
                <Td colSpan={9} className="py-8 text-center text-muted">
                  {(sensors || []).length === 0
                    ? <>No sensors yet — click <span className="font-medium text-text">Add Sensor</span> to register the first one.</>
                    : 'No sensors match this site filter.'}
                </Td>
              </Tr>
            )}
            {visibleSensors.map((s) => (
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
                  <Badge variant={statusVariant[s.status]} title={s.status_reason || undefined}>{s.status}</Badge>
                  {s.status_reason && <div className="mt-1 max-w-48 text-xs text-muted">{s.status_reason}</div>}
                </Td>
                <Td className="text-sm">{s.site_name || '—'}</Td>
                <Td className="text-sm">{s.location || '—'}</Td>
                <Td className="text-xs text-muted">
                  {relativeTime(s.last_heartbeat_at)}
                </Td>
                <Td className="text-xs tabular-nums">
                  {s.queue_depth} queued
                  {s.queue_dropped_count > 0 && <div className="text-danger">{s.queue_dropped_count} dropped</div>}
                </Td>
                <Td className="text-xs">
                  {s.version || '—'}
                  {isVersionBehind(s.version, s.min_supported_version) && (
                    <div className="mt-1"><Badge variant="warning">update required</Badge></div>
                  )}
                </Td>
                <Td className="text-xs">{s.assignment_count}</Td>
                <Td>
                  <div className="flex justify-end gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => loadDownloads.mutate(s)}
                      disabled={loadDownloads.isPending}
                      title="Download sensor appliance"
                    >
                      {loadDownloads.isPending ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Download className="h-3.5 w-3.5" />
                      )}
                      Download
                    </Button>
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

      <SensorDownloadsDialog
        info={downloads}
        onClose={() => setDownloads(null)}
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

function formatSize(bytes: number | null): string {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function ApplianceDownloadPanel({ manifest, loading }: { manifest?: ApplianceManifest; loading: boolean }) {
  const artifacts = manifest?.artifacts || []
  const byKind = Object.fromEntries(artifacts.map((a) => [a.kind, a])) as Partial<Record<ApplianceArtifact['kind'], ApplianceArtifact>>
  const availableCount = artifacts.filter((a) => a.available).length
  const hasAppliance = !!byKind.ova?.available || !!byKind.ovf?.available
  const isPreview = manifest?.status === 'preview'

  return (
    <div className="mb-4 rounded-lg border border-border bg-surface2/40 p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Download className="h-4 w-4 text-primary" />
            <div className="text-sm font-semibold">Sensor appliance download</div>
            <Badge variant={hasAppliance ? (isPreview ? 'warning' : 'success') : 'warning'}>
              {loading ? 'checking' : hasAppliance ? (isPreview ? 'preview' : 'ready') : 'not published'}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-muted">
            OVA/OVF images are served by this controller. Create a sensor to get the one-time token and bootstrap file.
          </p>
          {manifest && (
            <div className="mt-2 text-xs text-muted">
              {availableCount}/{artifacts.length} artifacts available from <span className="font-mono">{manifest.artifact_dir}</span>
            </div>
          )}
          {manifest?.note && (
            <div className="mt-2 text-xs text-warning">{manifest.note}</div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {byKind.ova && (
            <Button size="sm" variant="outline" asChild disabled={!byKind.ova.available}>
              <a href={byKind.ova.available ? byKind.ova.url : undefined} aria-disabled={!byKind.ova.available}>
                <Download className="h-3.5 w-3.5" /> OVA
              </a>
            </Button>
          )}
          {byKind.ovf && (
            <Button size="sm" variant="outline" asChild disabled={!byKind.ovf.available}>
              <a href={byKind.ovf.available ? byKind.ovf.url : undefined} aria-disabled={!byKind.ovf.available}>
                <Download className="h-3.5 w-3.5" /> OVF
              </a>
            </Button>
          )}
          {byKind.sha256 && (
            <Button size="sm" variant="ghost" asChild disabled={!byKind.sha256.available}>
              <a href={byKind.sha256.available ? byKind.sha256.url : undefined} aria-disabled={!byKind.sha256.available}>
                SHA256
              </a>
            </Button>
          )}
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        {(['ova', 'ovf', 'sha256'] as const).map((kind) => {
          const item = byKind[kind]
          return (
            <div key={kind} className="rounded-md border border-border bg-surface px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium uppercase tracking-wider text-muted">{kind}</span>
                <Badge variant={item?.available ? 'success' : 'outline'}>{item?.available ? 'available' : 'missing'}</Badge>
              </div>
              <div className="mt-1 truncate font-mono text-xs">{item?.filename || `zenplus-sensor.${kind}`}</div>
              <div className="mt-1 text-xs text-muted">{formatSize(item?.size_bytes ?? null)}</div>
            </div>
          )
        })}
      </div>
    </div>
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
  const [controllerUrl, setControllerUrl] = useState('')
  const [networkMode, setNetworkMode] = useState<'dhcp' | 'static'>('dhcp')
  const [sensorIp, setSensorIp] = useState('')
  const [sensorCidr, setSensorCidr] = useState('24')
  const [gateway, setGateway] = useState('')
  const [dnsServers, setDnsServers] = useState('')
  const [proxyUrl, setProxyUrl] = useState('')
  const [enableConsoleUser, setEnableConsoleUser] = useState(false)
  const [consoleUsername, setConsoleUsername] = useState('zenadmin')
  const [consolePassword, setConsolePassword] = useState('')

  useEffect(() => {
    if (!open) {
      setName(''); setDescription(''); setSiteId(''); setLocation('')
      setControllerUrl(''); setNetworkMode('dhcp'); setSensorIp(''); setSensorCidr('24'); setGateway(''); setDnsServers(''); setProxyUrl('')
      setEnableConsoleUser(false); setConsoleUsername('zenadmin'); setConsolePassword('')
    }
  }, [open])

  const create = useMutation({
    mutationFn: async () => {
      const payload: any = { name, description: description || null, location: location || null }
      if (siteId) payload.site_id = siteId
      if (controllerUrl) payload.controller_url = controllerUrl
      payload.network_mode = networkMode
      if (networkMode === 'static') {
        payload.sensor_ip = sensorIp
        payload.sensor_cidr = Number(sensorCidr)
        payload.gateway = gateway
        payload.dns_servers = dnsServers.split(',').map((s) => s.trim()).filter(Boolean)
      }
      if (proxyUrl) payload.proxy_url = proxyUrl
      payload.enable_console_user = enableConsoleUser
      if (enableConsoleUser) {
        payload.console_username = consoleUsername
        payload.console_password = consolePassword
      }
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
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
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
            <Select value={siteId || NO_SITE_VALUE} onValueChange={(v) => setSiteId(v === NO_SITE_VALUE ? '' : v)}>
              <SelectTrigger><SelectValue placeholder="(none)" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_SITE_VALUE}>(none)</SelectItem>
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
          <div className="grid gap-3 md:grid-cols-2">
            <FormField label="Controller URL" hint="Leave blank to use this controller URL">
              <Input value={controllerUrl} onChange={(e) => setControllerUrl(e.target.value)} placeholder="https://zenplus.example.com" />
            </FormField>
            <FormField label="Proxy URL" hint="Optional outbound proxy for sensor HTTPS">
              <Input value={proxyUrl} onChange={(e) => setProxyUrl(e.target.value)} placeholder="http://proxy.local:8080" />
            </FormField>
          </div>
          <div className="rounded-md border border-border bg-surface2/40 p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium">Local console user</div>
                <div className="text-xs text-muted">Created on first boot from the Seed ISO for VM CLI access.</div>
              </div>
              <Switch checked={enableConsoleUser} onCheckedChange={setEnableConsoleUser} />
            </div>
            {enableConsoleUser && (
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <FormField label="Username" required hint="Lowercase letters, numbers, dash, underscore">
                  <Input required value={consoleUsername} onChange={(e) => setConsoleUsername(e.target.value)} placeholder="zenadmin" />
                </FormField>
                <FormField label="Password" required hint="Minimum 8 characters">
                  <Input required type="password" value={consolePassword} onChange={(e) => setConsolePassword(e.target.value)} />
                </FormField>
              </div>
            )}
          </div>
          <FormField label="Sensor network">
            <Select value={networkMode} onValueChange={(v) => setNetworkMode(v as 'dhcp' | 'static')}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="dhcp">DHCP</SelectItem>
                <SelectItem value="static">Static IP</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {networkMode === 'static' && (
            <div className="grid gap-3 md:grid-cols-2">
              <FormField label="Sensor IP" required>
                <Input required value={sensorIp} onChange={(e) => setSensorIp(e.target.value)} placeholder="10.12.50.90" />
              </FormField>
              <FormField label="CIDR" required>
                <Input required type="number" min={1} max={sensorIp.includes(':') ? 128 : 32} value={sensorCidr} onChange={(e) => setSensorCidr(e.target.value)} />
              </FormField>
              <FormField label="Gateway" required>
                <Input required value={gateway} onChange={(e) => setGateway(e.target.value)} placeholder="10.12.50.1" />
              </FormField>
              <FormField label="DNS servers" hint="Comma separated">
                <Input value={dnsServers} onChange={(e) => setDnsServers(e.target.value)} placeholder="10.12.50.1, 8.8.8.8" />
              </FormField>
            </div>
          )}
          <FormField label="Description">
            <Textarea value={description} onChange={(e) => setDescription(e.target.value)} />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !name || (networkMode === 'static' && (!sensorIp || !sensorCidr || !gateway)) || (enableConsoleUser && (!consoleUsername || consolePassword.length < 8))}>
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
            {info.bootstrap_warning && (
              <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-xs text-danger">
                {info.bootstrap_warning} Use the Ubuntu install command below or repair the controller dependency and regenerate the token.
              </div>
            )}
            <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning">
              <strong>Token shown only once.</strong> Download the seed ISO and attach it to the sensor VM
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
                Appliance downloads
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                {info.manifest_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.manifest_url} target="_blank" rel="noreferrer">
                    <Download className="h-3.5 w-3.5" /> Manifest
                  </a>
                )}
                {info.ova_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.ova_url}>
                    <Download className="h-3.5 w-3.5" /> Base OVA
                  </a>
                )}
                {info.configured_ova_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-primary/50 bg-primary/10 px-3 py-2 text-xs text-primary hover:bg-primary/15" href={info.configured_ova_url}>
                    <Download className="h-3.5 w-3.5" /> Configured OVA
                  </a>
                )}
                {info.ovf_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.ovf_url}>
                    <Download className="h-3.5 w-3.5" /> OVF
                  </a>
                )}
                {info.bootstrap_iso_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.bootstrap_iso_url}>
                    <Download className="h-3.5 w-3.5" /> Seed ISO
                  </a>
                )}
              </div>
              <p className="mt-1 text-xs text-muted">
                Use Configured OVA when you want IP/controller settings baked into the VM. Use Base OVA plus Seed ISO for standard cloud-init deployment.
              </p>
            </div>

            {info.bootstrap_cloud_init && (
              <div>
                <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
                  Bootstrap cloud-init
                </div>
                <div className="rounded-md border border-border bg-surface2 p-3 font-mono text-xs leading-relaxed">
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all">{info.bootstrap_cloud_init}</pre>
                </div>
                <div className="mt-2 flex justify-end gap-2">
                  <Button size="sm" variant="outline" onClick={() => copyToClipboard(info.bootstrap_cloud_init || '', 'Bootstrap')}>
                    <Copy className="h-3.5 w-3.5" /> Copy
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => downloadText(`zenplus-sensor-${info.sensor_id}.yml`, info.bootstrap_cloud_init || '')}>
                    <Download className="h-3.5 w-3.5" /> Download
                  </Button>
                </div>
              </div>
            )}

            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
                One-line install command (Ubuntu sensor)
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

function SensorDownloadsDialog({ info, onClose }: { info: SensorDownloads | null; onClose: () => void }) {
  const hasConfiguredArtifacts = !!info?.configured_ova_url || !!info?.bootstrap_iso_url

  return (
    <Dialog open={!!info} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Download className="h-4 w-4 text-primary" />
            Download sensor appliance
          </DialogTitle>
        </DialogHeader>
        {info && (
          <div className="space-y-4">
            <div className="rounded-md border border-border bg-surface2 px-3 py-2">
              <div className="text-xs font-medium uppercase tracking-wider text-muted">Sensor</div>
              <div className="mt-1 font-medium text-text">{info.sensor_name}</div>
              {info.updated_at && (
                <div className="mt-1 text-xs text-muted">
                  Last generated {new Date(info.updated_at).toLocaleString()}
                </div>
              )}
            </div>

            {info.note && (
              <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning">
                {info.note}
              </div>
            )}

            <div>
              <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">
                Sensor-specific downloads
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {info.configured_ova_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-primary/50 bg-primary/10 px-3 py-2 text-xs text-primary hover:bg-primary/15" href={info.configured_ova_url}>
                    <Download className="h-3.5 w-3.5" /> Configured OVA
                    <span className="text-primary/70">{formatSize(info.configured_ova_size_bytes ?? null)}</span>
                  </a>
                )}
                {info.bootstrap_iso_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.bootstrap_iso_url}>
                    <Download className="h-3.5 w-3.5" /> Seed ISO
                    <span className="text-muted">{formatSize(info.bootstrap_iso_size_bytes ?? null)}</span>
                  </a>
                )}
                {!hasConfiguredArtifacts && (
                  <div className="rounded-md border border-border bg-surface2 px-3 py-4 text-center text-xs text-muted sm:col-span-2">
                    No configured files are available for this sensor.
                  </div>
                )}
              </div>
            </div>

            <div>
              <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">
                Generic appliance downloads
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                {info.manifest_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.manifest_url} target="_blank" rel="noreferrer">
                    <Download className="h-3.5 w-3.5" /> Manifest
                  </a>
                )}
                {info.ova_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.ova_url}>
                    <Download className="h-3.5 w-3.5" /> Base OVA
                  </a>
                )}
                {info.ovf_url && (
                  <a className="inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-surface2" href={info.ovf_url}>
                    <Download className="h-3.5 w-3.5" /> OVF
                  </a>
                )}
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

  const { data: events } = useQuery<SensorEvent[]>({
    queryKey: ['sensor-events', sensor?.id],
    enabled: !!sensor?.id,
    queryFn: async () => (await api.get(`/sensors/${sensor!.id}/events?limit=20`)).data,
    refetchInterval: open ? 10_000 : false,
  })

  const { data: commands } = useQuery<SensorCommand[]>({
    queryKey: ['sensor-commands', sensor?.id],
    enabled: !!sensor?.id,
    queryFn: async () => (await api.get(`/sensors/${sensor!.id}/commands?limit=20`)).data,
    refetchInterval: open ? 5_000 : false,
  })

  const { data: updateManifest } = useQuery<Record<string, unknown>>({
    queryKey: ['sensor-binary-update-manifest'],
    enabled: open,
    queryFn: async () => (await api.get('/sensor/bin/linux-amd64/manifest.json')).data,
    staleTime: 60_000,
  })
  const signedUpdateAvailable = typeof updateManifest?.signed_manifest === 'string'

  const { data: vantages } = useQuery<SensorVantage[]>({
    queryKey: ['sensor-vantages', sensor?.id],
    enabled: !!sensor?.id,
    queryFn: async () => (await api.get(`/sensors/${sensor!.id}/vantages`)).data,
    refetchInterval: open ? 10_000 : false,
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

  const { data: deviceGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['device-groups'],
    enabled: open,
    queryFn: async () => (await api.get('/devices/groups')).data,
  })

  const regen = useMutation({
    mutationFn: async () => (await api.post(`/sensors/${sensor!.id}/regenerate-token`)).data,
    onSuccess: (t) => { onShowToken(t); onChanged() },
    onError: (e: any) => toast.error('Token regen failed', apiErrorMessage(e)),
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

  const queueCommand = useMutation({
    mutationFn: async ({ verb, payload = {} }: { verb: SensorCommand['verb']; payload?: Record<string, unknown> }) =>
      (await api.post(`/sensors/${sensor!.id}/commands`, { verb, payload })).data,
    onSuccess: (_data, variables) => {
      toast.success(`${variables.verb.replace(/_/g, ' ')} queued`)
      qc.invalidateQueries({ queryKey: ['sensor-commands', sensor!.id] })
    },
    onError: (e: any) => toast.error('Command failed', apiErrorMessage(e)),
  })

  // Local editable state for assignments
  const [editing, setEditing] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [logLevel, setLogLevel] = useState('info')
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
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{sensor.name}</DialogTitle>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3 text-sm">
          <Row k="Status" v={<Badge variant={statusVariant[sensor.status]} title={sensor.status_reason || undefined}>{sensor.status}</Badge>} />
          <Row k="Status reason" v={sensor.status_reason || '—'} />
          <Row k="Site" v={sensor.site_name || '—'} />
          <Row k="Location" v={sensor.location || '—'} />
          <Row k="Last heartbeat" v={relativeTime(sensor.last_heartbeat_at)} />
          <Row k="Version" v={sensor.version || '—'} />
          <Row k="Hostname" v={sensor.hostname || '—'} />
          <Row k="Last IP" v={sensor.last_ip || '—'} />
          <Row k="Queue depth" v={String(sensor.queue_depth)} />
          <Row k="Dropped results" v={String(sensor.queue_dropped_count)} />
          <Row k="Health policy" v={`${sensor.degraded_after_s}s degraded · ${sensor.offline_after_s}s offline`} />
          <Row k="Minimum version" v={sensor.min_supported_version || '—'} />
          <Row k="API key" v={sensor.api_key_prefix ? `${sensor.api_key_prefix}…` : '— (not enrolled)'} />
          <Row k="Created" v={new Date(sensor.created_at).toLocaleString()} />
        </div>

        <div className="my-4 border-t border-border" />

        <div>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-medium">Remote commands</div>
              <div className="text-xs text-muted">Delivered over the next heartbeat; outcomes are retained below.</div>
            </div>
            <div className="flex flex-wrap justify-end gap-1">
              <Button size="sm" variant="outline" disabled={!sensor.api_key_prefix || sensor.status === 'disabled' || queueCommand.isPending} onClick={() => queueCommand.mutate({ verb: 'reload_config' })}>
                Reload config
              </Button>
              <Button size="sm" variant="outline" disabled={!sensor.api_key_prefix || sensor.status === 'disabled' || queueCommand.isPending} onClick={() => queueCommand.mutate({ verb: 'flush_buffer' })}>
                Drain buffer
              </Button>
              <Button size="sm" variant="outline" disabled={!signedUpdateAvailable || !sensor.api_key_prefix || sensor.status === 'disabled' || queueCommand.isPending} onClick={() => queueCommand.mutate({ verb: 'update' })}>
                <Download className="h-3.5 w-3.5" /> Upgrade
              </Button>
            </div>
          </div>
          <div className="mb-2 flex items-center gap-2">
            <Select value={logLevel} onValueChange={setLogLevel}>
              <SelectTrigger className="h-8 w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                {['debug', 'info', 'warn', 'error'].map((level) => <SelectItem key={level} value={level}>{level}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button size="sm" variant="outline" disabled={!sensor.api_key_prefix || sensor.status === 'disabled' || queueCommand.isPending} onClick={() => queueCommand.mutate({ verb: 'set_log_level', payload: { level: logLevel } })}>
              Set log level
            </Button>
            {!signedUpdateAvailable && <span className="text-xs text-muted">No release-signed update is published.</span>}
          </div>
          <div className="max-h-36 overflow-y-auto rounded-md border border-border">
            {(commands || []).length === 0 ? (
              <div className="px-3 py-4 text-center text-xs text-muted">No commands queued</div>
            ) : (
              <ul className="divide-y divide-border">
                {(commands || []).map((command) => (
                  <li key={command.id} className="flex items-start justify-between gap-3 px-3 py-2 text-xs">
                    <div className="min-w-0">
                      <div className="font-medium text-text">{command.verb.replace(/_/g, ' ')}</div>
                      {command.result && <div className="truncate text-muted" title={command.result}>{command.result}</div>}
                    </div>
                    <div className="shrink-0 text-right">
                      <Badge variant={command.status === 'succeeded' ? 'success' : command.status === 'failed' || command.status === 'expired' ? 'danger' : 'outline'}>{command.status}</Badge>
                      <div className="mt-1 text-muted">{relativeTime(command.completed_at || command.last_delivered_at || command.created_at)}</div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="my-4 border-t border-border" />

        <div>
          <div className="mb-2 text-sm font-medium">Service-check vantages</div>
          <div className="max-h-40 overflow-y-auto rounded-md border border-border">
            {(vantages || []).length === 0 ? (
              <div className="px-3 py-4 text-center text-xs text-muted">No service results from this sensor yet</div>
            ) : (
              <ul className="divide-y divide-border">
                {(vantages || []).map((vantage) => (
                  <li key={vantage.service_check_id} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                    <div className="min-w-0">
                      <div className="truncate font-medium text-text">{vantage.service_check_name}</div>
                      <div className="truncate text-muted">
                        {vantage.check_type} · {vantage.last_latency_ms == null ? '—' : `${vantage.last_latency_ms.toFixed(1)} ms`}
                        {vantage.tls_days_remaining == null ? '' : ` · TLS ${vantage.tls_days_remaining}d`}
                      </div>
                    </div>
                    <div className="text-right">
                      <Badge variant={statusVariant[vantage.state as keyof typeof statusVariant] || 'outline'}>{vantage.state}</Badge>
                      <div className="mt-1 text-muted">{relativeTime(vantage.last_result_at)}</div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="my-4 border-t border-border" />

        <div>
          <div className="mb-2 text-sm font-medium">Recent events</div>
          <div className="max-h-40 overflow-y-auto rounded-md border border-border">
            {(events || []).length === 0 ? (
              <div className="px-3 py-4 text-center text-xs text-muted">No lifecycle events yet</div>
            ) : (
              <ul className="divide-y divide-border">
                {(events || []).map((event) => (
                  <li key={event.id} className="flex items-start justify-between gap-3 px-3 py-2 text-xs">
                    <div>
                      <div className="font-medium text-text">{event.kind.replace(/_/g, ' ')}</div>
                      {typeof event.detail.reason === 'string' && (
                        <div className="mt-0.5 text-muted">{event.detail.reason}</div>
                      )}
                    </div>
                    <span className="shrink-0 text-muted">{relativeTime(event.ts)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
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
              groups={deviceGroups || []}
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
  devices, groups, serviceChecks, picked, onToggle,
}: {
  devices: Device[]
  groups: DeviceGroup[]
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
  const filteredGroups = useMemo(
    () => (f ? groups.filter((g) => g.name.toLowerCase().includes(f)) : groups),
    [groups, f],
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
        {filteredGroups.length > 0 && (
          <>
            <div className="mt-2 px-1 pb-1 text-xs font-medium uppercase tracking-wider text-muted">Device Groups</div>
            {filteredGroups.map((g) => {
              const key = `group:${g.id}`
              return (
                <label key={key} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-surface2">
                  <input type="checkbox" checked={picked.has(key)} onChange={() => onToggle(key)} />
                  <span className="text-sm">{g.name}</span>
                  <Badge variant="outline" className="ml-auto">group</Badge>
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
        {filteredDevices.length === 0 && filteredGroups.length === 0 && filteredChecks.length === 0 && (
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
