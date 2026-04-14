import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'

type DeviceState = {
  hostname: string
  ip_address: string
  device_type: string
  location: string
  description: string
  group_id: string
  ping_enabled: boolean
  ping_interval: number
  snmp_enabled: boolean
  snmp_version: '1' | '2c' | '3'
  snmp_port: number
  snmp_community: string
  snmp_v3_username: string
  snmp_v3_context: string
  snmp_auth_protocol: string
  snmp_auth_passphrase: string
  snmp_priv_protocol: string
  snmp_priv_passphrase: string
  snmp_timeout_ms: number
  snmp_retries: number
  snmp_poll_interval: number
}

const empty: DeviceState = {
  hostname: '',
  ip_address: '',
  device_type: 'other',
  location: '',
  description: '',
  group_id: '',
  ping_enabled: true,
  ping_interval: 60,
  snmp_enabled: false,
  snmp_version: '2c',
  snmp_port: 161,
  snmp_community: 'public',
  snmp_v3_username: '',
  snmp_v3_context: '',
  snmp_auth_protocol: '',
  snmp_auth_passphrase: '',
  snmp_priv_protocol: '',
  snmp_priv_passphrase: '',
  snmp_timeout_ms: 2000,
  snmp_retries: 2,
  snmp_poll_interval: 60,
}

export function DeviceFormDialog({
  open,
  onOpenChange,
  device,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  device?: any
}) {
  const isEdit = !!device?.id
  const qc = useQueryClient()
  const [state, setState] = useState<DeviceState>(empty)

  const { data: groups } = useQuery<any[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })

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
      })
    } else {
      setState(empty)
    }
  }, [open, device])

  const save = useMutation({
    mutationFn: async (payload: any) => {
      if (isEdit) {
        return (await api.put(`/devices/${device.id}`, payload)).data
      }
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
      hostname: state.hostname,
      ip_address: state.ip_address,
      device_type: state.device_type,
      location: state.location || null,
      description: state.description || null,
      group_id: state.group_id || null,
      ping_enabled: state.ping_enabled,
      ping_interval: state.ping_interval,
      snmp_enabled: state.snmp_enabled,
      snmp_version: state.snmp_version,
      snmp_port: state.snmp_port,
      snmp_timeout_ms: state.snmp_timeout_ms,
      snmp_retries: state.snmp_retries,
      snmp_poll_interval: state.snmp_poll_interval,
    }
    if (state.snmp_enabled) {
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
    save.mutate(payload)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit device' : 'Add device'}</DialogTitle>
          <DialogDescription>
            {isEdit ? 'Update monitoring configuration.' : 'Add a new device to start monitoring.'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <Tabs defaultValue="basic">
            <TabsList>
              <TabsTrigger value="basic">Basic</TabsTrigger>
              <TabsTrigger value="monitoring">Monitoring</TabsTrigger>
              <TabsTrigger value="snmp">SNMP</TabsTrigger>
            </TabsList>

            <TabsContent value="basic" className="grid grid-cols-2 gap-3">
              <FormField label="Hostname" required className="col-span-2">
                <Input
                  required
                  value={state.hostname}
                  onChange={(e) => setState({ ...state, hostname: e.target.value })}
                  placeholder="core-router-01"
                />
              </FormField>
              <FormField label="IP address" required>
                <Input
                  required
                  value={state.ip_address}
                  onChange={(e) => setState({ ...state, ip_address: e.target.value })}
                  placeholder="192.168.1.1"
                />
              </FormField>
              <FormField label="Device type">
                <Select
                  value={state.device_type}
                  onValueChange={(v) => setState({ ...state, device_type: v })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="router">Router</SelectItem>
                    <SelectItem value="switch">Switch</SelectItem>
                    <SelectItem value="firewall">Firewall</SelectItem>
                    <SelectItem value="server">Server</SelectItem>
                    <SelectItem value="access_point">Access point</SelectItem>
                    <SelectItem value="printer">Printer</SelectItem>
                    <SelectItem value="other">Other</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Location">
                <Input
                  value={state.location}
                  onChange={(e) => setState({ ...state, location: e.target.value })}
                  placeholder="DC-1 Rack A3"
                />
              </FormField>
              <FormField label="Group">
                <Select
                  value={state.group_id || '__none__'}
                  onValueChange={(v) => setState({ ...state, group_id: v === '__none__' ? '' : v })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="No group" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No group</SelectItem>
                    {(groups || []).map((g: any) => (
                      <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Description" className="col-span-2">
                <Textarea
                  value={state.description}
                  onChange={(e) => setState({ ...state, description: e.target.value })}
                  placeholder="Optional notes"
                />
              </FormField>
            </TabsContent>

            <TabsContent value="monitoring" className="space-y-4">
              <div className="flex items-center justify-between rounded-md border border-border p-3">
                <div>
                  <div className="text-sm font-medium">Ping monitoring</div>
                  <div className="text-xs text-muted">ICMP reachability + RTT + jitter</div>
                </div>
                <Switch
                  checked={state.ping_enabled}
                  onCheckedChange={(v) => setState({ ...state, ping_enabled: v })}
                />
              </div>
              <FormField label="Ping interval (seconds)">
                <Input
                  type="number"
                  min={10}
                  max={3600}
                  value={state.ping_interval}
                  onChange={(e) => setState({ ...state, ping_interval: Number(e.target.value) })}
                  disabled={!state.ping_enabled}
                />
              </FormField>
            </TabsContent>

            <TabsContent value="snmp" className="space-y-4">
              <div className="flex items-center justify-between rounded-md border border-border p-3">
                <div>
                  <div className="text-sm font-medium">SNMP polling</div>
                  <div className="text-xs text-muted">Poll interfaces, CPU, memory, sensors</div>
                </div>
                <Switch
                  checked={state.snmp_enabled}
                  onCheckedChange={(v) => setState({ ...state, snmp_enabled: v })}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Version">
                  <Select
                    value={state.snmp_version}
                    onValueChange={(v) => setState({ ...state, snmp_version: v as any })}
                  >
                    <SelectTrigger disabled={!state.snmp_enabled}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="1">v1</SelectItem>
                      <SelectItem value="2c">v2c</SelectItem>
                      <SelectItem value="3">v3</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Port">
                  <Input
                    type="number"
                    min={1}
                    max={65535}
                    value={state.snmp_port}
                    onChange={(e) => setState({ ...state, snmp_port: Number(e.target.value) })}
                    disabled={!state.snmp_enabled}
                  />
                </FormField>
                <FormField label="Timeout (ms)">
                  <Input
                    type="number"
                    min={100}
                    max={60000}
                    value={state.snmp_timeout_ms}
                    onChange={(e) => setState({ ...state, snmp_timeout_ms: Number(e.target.value) })}
                    disabled={!state.snmp_enabled}
                  />
                </FormField>
                <FormField label="Poll interval (s)">
                  <Input
                    type="number"
                    min={30}
                    max={3600}
                    value={state.snmp_poll_interval}
                    onChange={(e) => setState({ ...state, snmp_poll_interval: Number(e.target.value) })}
                    disabled={!state.snmp_enabled}
                  />
                </FormField>
              </div>

              {state.snmp_version !== '3' ? (
                <FormField label="Community">
                  <Input
                    type="password"
                    value={state.snmp_community}
                    onChange={(e) => setState({ ...state, snmp_community: e.target.value })}
                    disabled={!state.snmp_enabled}
                  />
                </FormField>
              ) : (
                <div className="space-y-3 rounded-md border border-border p-3">
                  <div className="text-xs font-semibold uppercase tracking-wider text-muted">
                    SNMPv3 credentials
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label="Username">
                      <Input
                        value={state.snmp_v3_username}
                        onChange={(e) => setState({ ...state, snmp_v3_username: e.target.value })}
                        disabled={!state.snmp_enabled}
                      />
                    </FormField>
                    <FormField label="Context">
                      <Input
                        value={state.snmp_v3_context}
                        onChange={(e) => setState({ ...state, snmp_v3_context: e.target.value })}
                        disabled={!state.snmp_enabled}
                      />
                    </FormField>
                    <FormField label="Auth protocol">
                      <Select
                        value={state.snmp_auth_protocol || '__none__'}
                        onValueChange={(v) =>
                          setState({ ...state, snmp_auth_protocol: v === '__none__' ? '' : v })
                        }
                      >
                        <SelectTrigger disabled={!state.snmp_enabled}>
                          <SelectValue placeholder="None" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none__">None</SelectItem>
                          <SelectItem value="MD5">MD5</SelectItem>
                          <SelectItem value="SHA">SHA</SelectItem>
                          <SelectItem value="SHA224">SHA-224</SelectItem>
                          <SelectItem value="SHA256">SHA-256</SelectItem>
                          <SelectItem value="SHA384">SHA-384</SelectItem>
                          <SelectItem value="SHA512">SHA-512</SelectItem>
                        </SelectContent>
                      </Select>
                    </FormField>
                    <FormField
                      label="Auth passphrase"
                      hint={isEdit && device.snmp_auth_configured ? 'Leave blank to keep existing' : undefined}
                    >
                      <Input
                        type="password"
                        value={state.snmp_auth_passphrase}
                        onChange={(e) => setState({ ...state, snmp_auth_passphrase: e.target.value })}
                        disabled={!state.snmp_enabled}
                        placeholder={isEdit && device.snmp_auth_configured ? '••••••••' : ''}
                      />
                    </FormField>
                    <FormField label="Priv protocol">
                      <Select
                        value={state.snmp_priv_protocol || '__none__'}
                        onValueChange={(v) =>
                          setState({ ...state, snmp_priv_protocol: v === '__none__' ? '' : v })
                        }
                      >
                        <SelectTrigger disabled={!state.snmp_enabled}>
                          <SelectValue placeholder="None" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none__">None</SelectItem>
                          <SelectItem value="DES">DES</SelectItem>
                          <SelectItem value="AES">AES-128</SelectItem>
                          <SelectItem value="AES192">AES-192</SelectItem>
                          <SelectItem value="AES256">AES-256</SelectItem>
                        </SelectContent>
                      </Select>
                    </FormField>
                    <FormField
                      label="Priv passphrase"
                      hint={isEdit && device.snmp_priv_configured ? 'Leave blank to keep existing' : undefined}
                    >
                      <Input
                        type="password"
                        value={state.snmp_priv_passphrase}
                        onChange={(e) => setState({ ...state, snmp_priv_passphrase: e.target.value })}
                        disabled={!state.snmp_enabled}
                        placeholder={isEdit && device.snmp_priv_configured ? '••••••••' : ''}
                      />
                    </FormField>
                  </div>
                </div>
              )}
            </TabsContent>
          </Tabs>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
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
