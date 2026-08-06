import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Key, Layers, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { Badge } from '@/components/ui/Badge'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'

type Credential = {
  id: string; name: string; snmp_version: string;
  community: string | null; v3_username: string | null;
  v3_auth_protocol: string | null; v3_priv_protocol: string | null;
  port: number; timeout_ms: number; retries: number; is_default: boolean
}

type DeviceState = {
  hostname: string; ip_address: string; device_type: string;
  location: string; description: string; group_id: string;
  ping_enabled: boolean; ping_interval: number;
  snmp_enabled: boolean; snmp_credential_mode: 'saved' | 'manual';
  snmp_credential_id: string;
  snmp_version: '1' | '2c' | '3'; snmp_port: number;
  snmp_community: string; snmp_v3_username: string; snmp_v3_context: string;
  snmp_auth_protocol: string; snmp_auth_passphrase: string;
  snmp_priv_protocol: string; snmp_priv_passphrase: string;
  snmp_timeout_ms: number; snmp_retries: number;
  snmp_poll_interval: number; snmp_max_repetitions: number;
  profile_id: string;
}

const empty: DeviceState = {
  hostname: '', ip_address: '', device_type: 'other',
  location: '', description: '', group_id: '',
  ping_enabled: true, ping_interval: 60,
  snmp_enabled: false, snmp_credential_mode: 'saved', snmp_credential_id: '',
  snmp_version: '2c', snmp_port: 161, snmp_community: 'public',
  snmp_v3_username: '', snmp_v3_context: '',
  snmp_auth_protocol: '', snmp_auth_passphrase: '',
  snmp_priv_protocol: '', snmp_priv_passphrase: '',
  snmp_timeout_ms: 2000, snmp_retries: 2,
  snmp_poll_interval: 60, snmp_max_repetitions: 25,
  profile_id: '',
}

export function DeviceFormDialog({
  open, onOpenChange, device,
}: {
  open: boolean; onOpenChange: (o: boolean) => void; device?: any
}) {
  const isEdit = !!device?.id
  const qc = useQueryClient()
  const [state, setState] = useState<DeviceState>(empty)

  const { data: groups } = useQuery<any[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })

  const { data: credentials } = useQuery<Credential[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
    enabled: open,
  })

  const { data: templates } = useQuery<any[]>({
    queryKey: ['snmp-profiles'],
    queryFn: async () => (await api.get('/snmp/profiles')).data,
    enabled: open,
  })

  const defaultCred = credentials?.find((c) => c.is_default)

  useEffect(() => {
    if (!open) return
    if (device) {
      setState({
        ...empty,
        hostname: device.hostname || '',
        ip_address: device.ip_address || '',
        device_type: device.device_type || 'other',
        location: device.location || '',
        description: device.description || '',
        group_id: device.group_id || '',
        ping_enabled: device.ping_enabled ?? true,
        ping_interval: device.ping_interval || 60,
        snmp_enabled: device.snmp_enabled ?? false,
        snmp_credential_mode: device.snmp_credential_id ? 'saved' : 'manual',
        snmp_credential_id: device.snmp_credential_id || '',
        snmp_version: device.snmp_version || '2c',
        snmp_port: device.snmp_port || 161,
        snmp_community: device.snmp_community || '',
        snmp_v3_username: device.snmp_v3_username || '',
        snmp_v3_context: device.snmp_v3_context || '',
        snmp_auth_protocol: device.snmp_auth_protocol || '',
        snmp_auth_passphrase: '',
        snmp_priv_protocol: device.snmp_priv_protocol || '',
        snmp_priv_passphrase: '',
        snmp_timeout_ms: device.snmp_timeout_ms || 2000,
        snmp_retries: device.snmp_retries ?? 2,
        snmp_poll_interval: device.snmp_poll_interval || 60,
        snmp_max_repetitions: device.snmp_max_repetitions || 25,
        profile_id: device.profile_id || '',
      })
    } else {
      setState({
        ...empty,
        snmp_credential_id: defaultCred?.id || '',
        snmp_credential_mode: defaultCred ? 'saved' : 'manual',
      })
    }
  }, [open, device, defaultCred?.id])

  const save = useMutation({
    mutationFn: async (payload: any) => {
      if (isEdit) return (await api.put(`/devices/${device.id}`, payload)).data
      return (await api.post('/devices', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Device updated' : 'Device created')
      qc.invalidateQueries({ queryKey: ['devices'] })
      if (isEdit) qc.invalidateQueries({ queryKey: ['device', device.id] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    const payload: any = {
      hostname: state.hostname, ip_address: state.ip_address,
      device_type: state.device_type, location: state.location || null,
      description: state.description || null, group_id: state.group_id || null,
      ping_enabled: state.ping_enabled, ping_interval: state.ping_interval,
      snmp_enabled: state.snmp_enabled, snmp_poll_interval: state.snmp_poll_interval,
      profile_id: state.profile_id || null,
    }

    if (state.snmp_enabled) {
      if (state.snmp_credential_mode === 'saved' && state.snmp_credential_id) {
        // Use saved credential — backend applies the credential settings
        payload.snmp_credential_id = state.snmp_credential_id
        const cred = credentials?.find((c) => c.id === state.snmp_credential_id)
        if (cred) {
          payload.snmp_version = cred.snmp_version
          payload.snmp_port = cred.port
          payload.snmp_timeout_ms = cred.timeout_ms
          payload.snmp_retries = cred.retries
          if (cred.snmp_version !== '3') {
            payload.snmp_community = cred.community || 'public'
          }
        }
      } else {
        // Manual inline credentials — drop any previously-linked credential
        payload.snmp_credential_id = null
        payload.snmp_version = state.snmp_version
        payload.snmp_port = state.snmp_port
        payload.snmp_timeout_ms = state.snmp_timeout_ms
        payload.snmp_retries = state.snmp_retries
        payload.snmp_max_repetitions = state.snmp_max_repetitions
        if (state.snmp_version === '3') {
          payload.snmp_v3_username = state.snmp_v3_username || null
          payload.snmp_v3_context = state.snmp_v3_context || null
          payload.snmp_auth_protocol = state.snmp_auth_protocol || null
          payload.snmp_priv_protocol = state.snmp_priv_protocol || null
          if (state.snmp_auth_passphrase) payload.snmp_auth_passphrase = state.snmp_auth_passphrase
          if (state.snmp_priv_passphrase) payload.snmp_priv_passphrase = state.snmp_priv_passphrase
        } else {
          payload.snmp_community = state.snmp_community || 'public'
        }
      }
    }
    save.mutate(payload)
  }

  const selectedCred = credentials?.find((c) => c.id === state.snmp_credential_id)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit device' : 'Add device'}</DialogTitle>
          <DialogDescription>{isEdit ? 'Update monitoring configuration.' : 'Add a new device to start monitoring.'}</DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <Tabs defaultValue="basic">
            <TabsList>
              <TabsTrigger value="basic">Basic</TabsTrigger>
              <TabsTrigger value="monitoring">Monitoring</TabsTrigger>
              <TabsTrigger value="snmp">SNMP</TabsTrigger>
            </TabsList>

            {/* === Basic tab === */}
            <TabsContent value="basic" className="grid grid-cols-2 gap-3">
              <FormField label="Hostname" required className="col-span-2">
                <Input required value={state.hostname} onChange={(e) => setState({ ...state, hostname: e.target.value })} placeholder="core-router-01" />
              </FormField>
              <FormField label="IP address" required>
                <Input required value={state.ip_address} onChange={(e) => setState({ ...state, ip_address: e.target.value })} placeholder="192.168.1.1" />
              </FormField>
              <FormField label="Device type">
                <Select value={state.device_type} onValueChange={(v) => setState({ ...state, device_type: v })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {['router', 'switch', 'firewall', 'server', 'access_point', 'printer', 'other'].map((t) => (
                      <SelectItem key={t} value={t}>{t.replace('_', ' ')}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Location">
                <Input value={state.location} onChange={(e) => setState({ ...state, location: e.target.value })} placeholder="DC-1 Rack A3" />
              </FormField>
              <FormField label="Group">
                <Select value={state.group_id || '__none__'} onValueChange={(v) => setState({ ...state, group_id: v === '__none__' ? '' : v })}>
                  <SelectTrigger><SelectValue placeholder="No group" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No group</SelectItem>
                    {(groups || []).map((g: any) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Description" className="col-span-2">
                <Textarea value={state.description} onChange={(e) => setState({ ...state, description: e.target.value })} placeholder="Optional notes" />
              </FormField>
            </TabsContent>

            {/* === Monitoring tab === */}
            <TabsContent value="monitoring" className="space-y-4">
              <div className="flex items-center justify-between rounded-md border border-border p-3">
                <div><div className="text-sm font-medium">Ping monitoring</div><div className="text-xs text-muted">ICMP reachability + RTT + jitter</div></div>
                <Switch checked={state.ping_enabled} onCheckedChange={(v) => setState({ ...state, ping_enabled: v })} />
              </div>
              <FormField label="Ping interval (seconds)">
                <Input type="number" min={10} max={3600} value={state.ping_interval} onChange={(e) => setState({ ...state, ping_interval: Number(e.target.value) })} disabled={!state.ping_enabled} />
              </FormField>
            </TabsContent>

            {/* === SNMP tab === */}
            <TabsContent value="snmp" className="space-y-4">
              <div className="flex items-center justify-between rounded-md border border-border p-3">
                <div><div className="text-sm font-medium">SNMP polling</div><div className="text-xs text-muted">Poll interfaces, CPU, memory, sensors via SNMP</div></div>
                <Switch checked={state.snmp_enabled} onCheckedChange={(v) => setState({ ...state, snmp_enabled: v })} />
              </div>

              {state.snmp_enabled && (
                <>
                  {/* Credential source selector */}
                  <div className="rounded-lg border border-border p-4">
                    <div className="mb-3 text-sm font-semibold">SNMP Credential</div>
                    <div className="mb-3 grid grid-cols-2 gap-2">
                      <button type="button" onClick={() => setState({ ...state, snmp_credential_mode: 'saved' })}
                        className={`rounded-md border-2 px-3 py-2 text-left text-sm transition ${state.snmp_credential_mode === 'saved' ? 'border-primary bg-primary/5' : 'border-border hover:border-border-strong'}`}>
                        <div className="flex items-center gap-1.5 font-medium"><Key className="h-3.5 w-3.5" /> Saved Credential</div>
                        <div className="text-[11px] text-muted">Select from credential store</div>
                      </button>
                      <button type="button" onClick={() => setState({ ...state, snmp_credential_mode: 'manual' })}
                        className={`rounded-md border-2 px-3 py-2 text-left text-sm transition ${state.snmp_credential_mode === 'manual' ? 'border-primary bg-primary/5' : 'border-border hover:border-border-strong'}`}>
                        <div className="font-medium">Manual Entry</div>
                        <div className="text-[11px] text-muted">Enter credentials inline</div>
                      </button>
                    </div>

                    {state.snmp_credential_mode === 'saved' ? (
                      <>
                        <Select value={state.snmp_credential_id || '__none__'} onValueChange={(v) => setState({ ...state, snmp_credential_id: v === '__none__' ? '' : v })}>
                          <SelectTrigger><SelectValue placeholder="Select credential..." /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="__none__">None selected</SelectItem>
                            {(credentials || []).map((c) => (
                              <SelectItem key={c.id} value={c.id}>
                                {c.name} <span className="text-muted">v{c.snmp_version}</span>
                                {c.is_default && <span className="text-warning ml-1">(default)</span>}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {selectedCred && (
                          <div className="mt-2 rounded-md bg-surface2 px-3 py-2 text-xs">
                            <div className="flex items-center gap-2">
                              <Badge variant="info">v{selectedCred.snmp_version}</Badge>
                              {selectedCred.snmp_version !== '3' ? (
                                <span>Community: ••••••••</span>
                              ) : (
                                <span>User: {selectedCred.v3_username} · {selectedCred.v3_auth_protocol || 'no auth'}{selectedCred.v3_priv_protocol ? `/${selectedCred.v3_priv_protocol}` : ''}</span>
                              )}
                              <span className="text-muted">Port {selectedCred.port}</span>
                            </div>
                          </div>
                        )}
                        {!state.snmp_credential_id && (
                          <p className="mt-2 text-xs text-warning">No credential selected — SNMP polling won't work without credentials</p>
                        )}
                      </>
                    ) : (
                      /* Manual credential entry */
                      <div className="space-y-3">
                        <div className="grid grid-cols-2 gap-3">
                          <FormField label="Version">
                            <Select value={state.snmp_version} onValueChange={(v) => setState({ ...state, snmp_version: v as any })}>
                              <SelectTrigger><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="1">v1</SelectItem>
                                <SelectItem value="2c">v2c</SelectItem>
                                <SelectItem value="3">v3</SelectItem>
                              </SelectContent>
                            </Select>
                          </FormField>
                          <FormField label="Port">
                            <Input type="number" min={1} max={65535} value={state.snmp_port} onChange={(e) => setState({ ...state, snmp_port: Number(e.target.value) })} />
                          </FormField>
                        </div>

                        {state.snmp_version !== '3' ? (
                          <FormField label="Community string">
                            <Input type="password" value={state.snmp_community} onChange={(e) => setState({ ...state, snmp_community: e.target.value })} />
                          </FormField>
                        ) : (
                          <div className="space-y-3 rounded-md border border-border p-3">
                            <div className="text-xs font-semibold uppercase tracking-wider text-muted">SNMPv3 USM</div>
                            <div className="grid grid-cols-2 gap-3">
                              <FormField label="Username"><Input value={state.snmp_v3_username} onChange={(e) => setState({ ...state, snmp_v3_username: e.target.value })} /></FormField>
                              <FormField label="Context"><Input value={state.snmp_v3_context} onChange={(e) => setState({ ...state, snmp_v3_context: e.target.value })} /></FormField>
                              <FormField label="Auth protocol">
                                <Select value={state.snmp_auth_protocol || '__none__'} onValueChange={(v) => setState({ ...state, snmp_auth_protocol: v === '__none__' ? '' : v })}>
                                  <SelectTrigger><SelectValue placeholder="None" /></SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value="__none__">None</SelectItem>
                                    {['MD5','SHA','SHA224','SHA256','SHA384','SHA512'].map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                                  </SelectContent>
                                </Select>
                              </FormField>
                              <FormField label="Auth passphrase" hint={isEdit && device?.snmp_auth_configured ? 'Leave blank to keep' : undefined}>
                                <Input type="password" value={state.snmp_auth_passphrase} onChange={(e) => setState({ ...state, snmp_auth_passphrase: e.target.value })} placeholder={isEdit && device?.snmp_auth_configured ? '••••••••' : ''} />
                              </FormField>
                              <FormField label="Priv protocol">
                                <Select value={state.snmp_priv_protocol || '__none__'} onValueChange={(v) => setState({ ...state, snmp_priv_protocol: v === '__none__' ? '' : v })}>
                                  <SelectTrigger><SelectValue placeholder="None" /></SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value="__none__">None</SelectItem>
                                    {['DES','AES','AES192','AES256'].map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                                  </SelectContent>
                                </Select>
                              </FormField>
                              <FormField label="Priv passphrase" hint={isEdit && device?.snmp_priv_configured ? 'Leave blank to keep' : undefined}>
                                <Input type="password" value={state.snmp_priv_passphrase} onChange={(e) => setState({ ...state, snmp_priv_passphrase: e.target.value })} placeholder={isEdit && device?.snmp_priv_configured ? '••••••••' : ''} />
                              </FormField>
                            </div>
                          </div>
                        )}

                        <div className="grid grid-cols-3 gap-3">
                          <FormField label="Timeout (ms)">
                            <Input type="number" min={200} max={30000} value={state.snmp_timeout_ms} onChange={(e) => setState({ ...state, snmp_timeout_ms: Number(e.target.value) })} />
                          </FormField>
                          <FormField label="Retries">
                            <Input type="number" min={0} max={10} value={state.snmp_retries} onChange={(e) => setState({ ...state, snmp_retries: Number(e.target.value) })} />
                          </FormField>
                          <FormField label="Max repetitions" hint="GETBULK size">
                            <Input type="number" min={1} max={200} value={state.snmp_max_repetitions} onChange={(e) => setState({ ...state, snmp_max_repetitions: Number(e.target.value) })} />
                          </FormField>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Poll interval (always visible) */}
                  <FormField label="Poll interval (seconds)" hint="How often to collect SNMP data">
                    <Input type="number" min={30} max={3600} value={state.snmp_poll_interval} onChange={(e) => setState({ ...state, snmp_poll_interval: Number(e.target.value) })} />
                  </FormField>

                  {/* Monitoring template */}
                  <div className="rounded-lg border border-border p-4">
                    <div className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
                      <Layers className="h-3.5 w-3.5 text-primary" /> Monitoring Template
                    </div>
                    <p className="mb-3 text-xs text-muted">
                      Vendor templates collect deep, device-specific insights (HA, VPN tunnels, SD-WAN,
                      managed APs/switches…) on top of standard monitoring. Leave on Default to
                      auto-detect from the device's SNMP identity.
                    </p>
                    <Select value={state.profile_id || '__default__'} onValueChange={(v) => setState({ ...state, profile_id: v === '__default__' ? '' : v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__default__">Default (auto-detect)</SelectItem>
                        {(templates || []).map((t: any) => (
                          <SelectItem key={t.id} value={t.id}>
                            {t.name}{t.vendor ? <span className="text-muted"> · {t.vendor}</span> : null}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )}
            </TabsContent>
          </Tabs>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create device'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
