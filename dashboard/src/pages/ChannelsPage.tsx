import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  BellRing,
  CheckCircle2,
  ExternalLink,
  FileText,
  Loader2,
  Mail,
  MessageSquare,
  Pencil,
  Phone,
  Plus,
  Send,
  Settings,
  ShieldCheck,
  Slack,
  Ticket,
  Trash2,
  Webhook,
  Workflow,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'

type ChannelType = 'email' | 'sms' | 'webhook' | 'slack' | 'telegram'
type GatewayType = 'smtp' | 'sms'

type Channel = {
  id: string
  name: string
  type: ChannelType
  config: Record<string, any>
  enabled: boolean
  gateway_id?: string | null
  gateway_name?: string | null
  created_at?: string | null
  updated_at?: string | null
}

type Gateway = {
  id: string
  name: string
  type: GatewayType
  enabled: boolean
  is_default: boolean
  config: Record<string, any>
}

type ChannelForm = {
  name: string
  type: ChannelType
  enabled: boolean
  config: Record<string, any>
}

const channelMeta: Record<ChannelType, {
  label: string
  gatewayLabel: string
  icon: React.ComponentType<{ className?: string }>
  tone: string
}> = {
  email: {
    label: 'Email',
    gatewayLabel: 'SMTP',
    icon: Mail,
    tone: 'border-info/30 bg-info/10 text-info',
  },
  sms: {
    label: 'SMS',
    gatewayLabel: 'SMS',
    icon: Phone,
    tone: 'border-success/30 bg-success/10 text-success',
  },
  webhook: {
    label: 'Webhook',
    gatewayLabel: 'HTTP',
    icon: Webhook,
    tone: 'border-primary/30 bg-primary/10 text-primary',
  },
  slack: {
    label: 'Slack',
    gatewayLabel: 'Webhook',
    icon: Slack,
    tone: 'border-warning/30 bg-warning/10 text-warning',
  },
  telegram: {
    label: 'Telegram',
    gatewayLabel: 'Bot',
    icon: MessageSquare,
    tone: 'border-border bg-surface2 text-text',
  },
}

const connectorCatalog: Array<{
  type?: ChannelType
  title: string
  subtitle: string
  status: 'ready' | 'planned'
  icon: React.ComponentType<{ className?: string }>
}> = [
  { type: 'email', title: 'Email / SMTP', subtitle: 'Ops mailboxes and distribution lists', status: 'ready', icon: Mail },
  { type: 'sms', title: 'SMS', subtitle: 'On-call phones and escalation groups', status: 'ready', icon: Phone },
  { type: 'webhook', title: 'Webhook', subtitle: 'Generic HTTP automation endpoints', status: 'ready', icon: Webhook },
  { type: 'slack', title: 'Slack', subtitle: 'Workspace incoming webhooks', status: 'ready', icon: Slack },
  { type: 'telegram', title: 'Telegram', subtitle: 'Bot chat delivery', status: 'ready', icon: MessageSquare },
  { title: 'Microsoft Teams', subtitle: 'Collaboration channel cards', status: 'planned', icon: MessageSquare },
  { title: 'Jira / ServiceNow', subtitle: 'Ticketing and incident workflow', status: 'planned', icon: Ticket },
]

const DEFAULT_FORM: ChannelForm = {
  name: '',
  type: 'email',
  enabled: true,
  config: {},
}

export function ChannelsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Channel | null>(null)
  const [deleting, setDeleting] = useState<Channel | null>(null)

  const { data: channels = [], isLoading: loadingChannels, error: channelsError } = useQuery<Channel[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => {
      const r = (await api.get('/settings/channels')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const { data: gateways = [], isLoading: loadingGateways } = useQuery<Gateway[]>({
    queryKey: ['settings', 'gateways-list'],
    queryFn: async () => {
      const r = (await api.get('/settings/gateways/list')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const test = useMutation({
    mutationFn: async (id: string) => (await api.post(`/settings/channels/${id}/test`)).data,
    onSuccess: (data) => toast.success('Test sent', data?.message ? String(data.message) : undefined),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/settings/channels/${id}`),
    onSuccess: () => {
      toast.success('Channel deleted')
      setDeleting(null)
      qc.invalidateQueries({ queryKey: ['settings', 'channels'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const summary = useMemo(() => {
    const enabled = channels.filter((ch) => ch.enabled).length
    const disabled = channels.length - enabled
    const gatewayReady = gateways.filter((gw) => gw.enabled).length
    const hasEmail = channels.some((ch) => ch.type === 'email' && ch.enabled)
    const hasSms = channels.some((ch) => ch.type === 'sms' && ch.enabled)
    return { enabled, disabled, gatewayReady, hasEmail, hasSms }
  }, [channels, gateways])

  function openCreate(type: ChannelType = 'email') {
    setEditing({ id: '', name: '', type, enabled: true, config: {}, gateway_id: null })
    setFormOpen(true)
  }

  function openEdit(channel: Channel) {
    setEditing(channel)
    setFormOpen(true)
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <BellRing className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">Channels</h1>
              <p className="text-xs text-muted">Messaging, alert delivery, reporting, and ticket routing endpoints.</p>
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => navigate('/gateways')}>
            <Settings className="h-4 w-4" />
            Gateways
          </Button>
          <Button onClick={() => openCreate()}>
            <Plus className="h-4 w-4" />
            New channel
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard icon={ShieldCheck} label="Enabled Channels" value={summary.enabled} sub={`${summary.disabled} disabled`} tone="success" />
        <MetricCard icon={Settings} label="Active Gateways" value={summary.gatewayReady} sub={`${gateways.length} configured`} tone="info" />
        <MetricCard icon={Workflow} label="Alert Routing" value={channels.length ? 'Ready' : 'Not set'} sub="Rules can target channels" tone={channels.length ? 'success' : 'warning'} />
        <MetricCard icon={Ticket} label="ITSM Routing" value="Planned" sub="Ticketing connector lane" tone="neutral" />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Card>
          <CardContent className="p-0">
            <div className="flex flex-col gap-2 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-sm font-semibold">Configured Channels</h2>
                <p className="text-xs text-muted">
                  {loadingChannels ? 'Loading channels' : `${channels.length} channel${channels.length === 1 ? '' : 's'} configured`}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <HealthPill ok={summary.hasEmail} label="Email" />
                <HealthPill ok={summary.hasSms} label="SMS" />
                <HealthPill ok={summary.gatewayReady > 0} label="Gateways" />
              </div>
            </div>

            {channelsError ? (
              <div className="m-4 rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                Failed to load channels: {apiErrorMessage(channelsError as any)}
              </div>
            ) : (
              <Table>
                <THead className="bg-surface2/60">
                  <Tr>
                    <Th>Channel</Th>
                    <Th>Destination</Th>
                    <Th>Gateway</Th>
                    <Th>Status</Th>
                    <Th>Updated</Th>
                    <Th className="w-44 text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {loadingChannels ? (
                    <Tr>
                      <Td colSpan={6} className="py-10 text-center text-sm text-muted">
                        <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />
                        Loading channels
                      </Td>
                    </Tr>
                  ) : channels.length === 0 ? (
                    <Tr>
                      <Td colSpan={6} className="py-12">
                        <div className="mx-auto flex max-w-md flex-col items-center gap-3 text-center">
                          <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10 text-primary">
                            <BellRing className="h-6 w-6" />
                          </div>
                          <div>
                            <div className="text-sm font-semibold">No channels configured</div>
                            <div className="mt-1 text-xs text-muted">Create the first destination for alerts and scheduled reports.</div>
                          </div>
                          <Button size="sm" onClick={() => openCreate()}>
                            <Plus className="h-4 w-4" />
                            New channel
                          </Button>
                        </div>
                      </Td>
                    </Tr>
                  ) : (
                    channels.map((channel) => {
                      const meta = channelMeta[channel.type] || channelMeta.webhook
                      const Icon = meta.icon
                      return (
                        <Tr key={channel.id} className="align-top">
                          <Td>
                            <div className="flex items-start gap-3">
                              <div className={cn('mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border', meta.tone)}>
                                <Icon className="h-4 w-4" />
                              </div>
                              <div className="min-w-0">
                                <div className="font-medium text-text">{channel.name}</div>
                                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                  <Badge variant="outline">{meta.label}</Badge>
                                  <span className="text-[11px] text-muted">{channel.id.slice(0, 8)}</span>
                                </div>
                              </div>
                            </div>
                          </Td>
                          <Td>
                            <div className="max-w-sm truncate text-sm">{destinationLabel(channel)}</div>
                            <div className="mt-1 text-[11px] text-muted">{secondaryDestinationLabel(channel)}</div>
                          </Td>
                          <Td>
                            <div className="text-sm">{channel.gateway_name || gatewayLabel(channel, gateways)}</div>
                            <div className="mt-1 text-[11px] text-muted">{meta.gatewayLabel}</div>
                          </Td>
                          <Td>
                            <Badge variant={channel.enabled ? 'success' : 'outline'}>
                              {channel.enabled ? 'Enabled' : 'Disabled'}
                            </Badge>
                          </Td>
                          <Td>
                            <div className="text-sm text-text2">{channel.updated_at ? relativeTime(channel.updated_at) : '-'}</div>
                            <div className="mt-1 text-[11px] text-muted">{channel.created_at ? `Created ${relativeTime(channel.created_at)}` : ''}</div>
                          </Td>
                          <Td>
                            <div className="flex justify-end gap-1">
                              <Button size="sm" variant="outline" onClick={() => test.mutate(channel.id)} disabled={test.isPending}>
                                {test.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                                Test
                              </Button>
                              <Button size="sm" variant="ghost" onClick={() => openEdit(channel)} title="Edit channel">
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
                              <Button size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={() => setDeleting(channel)} title="Delete channel">
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            </div>
                          </Td>
                        </Tr>
                      )
                    })
                  )}
                </TBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-3 p-4">
              <div>
                <h2 className="text-sm font-semibold">Connector Catalog</h2>
                <p className="text-xs text-muted">Available and planned delivery targets.</p>
              </div>
              <div className="space-y-2">
                {connectorCatalog.map((connector) => {
                  const Icon = connector.icon
                  const enabled = connector.status === 'ready'
                  return (
                    <button
                      key={connector.title}
                      type="button"
                      disabled={!enabled}
                      onClick={() => connector.type && openCreate(connector.type)}
                      className={cn(
                        'flex w-full items-center gap-3 rounded-lg border border-border bg-bg p-3 text-left transition-colors',
                        enabled ? 'hover:border-primary/40 hover:bg-primary/5' : 'cursor-not-allowed opacity-60',
                      )}
                    >
                      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface2 text-text2">
                        <Icon className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-sm font-medium">{connector.title}</span>
                          <Badge variant={enabled ? 'success' : 'outline'}>{enabled ? 'Ready' : 'Planned'}</Badge>
                        </div>
                        <div className="mt-1 truncate text-xs text-muted">{connector.subtitle}</div>
                      </div>
                    </button>
                  )
                })}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-3 p-4">
              <div>
                <h2 className="text-sm font-semibold">Routing Lanes</h2>
                <p className="text-xs text-muted">Channel usage by product area.</p>
              </div>
              <RoutingLane icon={AlertTriangle} label="Alerts" status={channels.length ? 'Ready' : 'Needs channel'} ready={channels.length > 0} />
              <RoutingLane icon={FileText} label="Reports" status="Ready for assignment" ready={channels.length > 0} />
              <RoutingLane icon={Ticket} label="Tickets" status="Connector planned" ready={false} />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-sm font-semibold">Gateway Layer</h2>
                  <p className="text-xs text-muted">
                    {loadingGateways ? 'Loading gateways' : `${gateways.length} SMTP/SMS gateway${gateways.length === 1 ? '' : 's'}`}
                  </p>
                </div>
                <Button size="sm" variant="outline" onClick={() => navigate('/gateways')}>
                  <ExternalLink className="h-3.5 w-3.5" />
                  Open
                </Button>
              </div>
              <div className="space-y-2">
                {gateways.length === 0 ? (
                  <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
                    No gateway records found. Configure SMTP or SMS before testing email and SMS channels.
                  </div>
                ) : (
                  gateways.slice(0, 4).map((gateway) => (
                    <div key={gateway.id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-3 py-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{gateway.name || `${gateway.type.toUpperCase()} gateway`}</div>
                        <div className="text-[11px] text-muted">{gateway.type.toUpperCase()} {gateway.is_default ? 'default' : ''}</div>
                      </div>
                      <Badge variant={gateway.enabled ? 'success' : 'outline'}>{gateway.enabled ? 'Enabled' : 'Disabled'}</Badge>
                    </div>
                  ))
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>

      <ChannelFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        channel={editing}
        gateways={gateways}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Delete channel"
        description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => deleting && del.mutate(deleting.id)}
      />
    </div>
  )
}

function ChannelFormDialog({
  open,
  onOpenChange,
  channel,
  gateways,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  channel: Channel | null
  gateways: Gateway[]
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState<ChannelForm>(DEFAULT_FORM)
  const isEdit = !!channel?.id

  useEffect(() => {
    if (!open) return
    if (!channel) {
      setForm(DEFAULT_FORM)
      return
    }
    const config = { ...(channel.config || {}) }
    if (channel.gateway_id && !config.gateway_id) config.gateway_id = channel.gateway_id
    if (channel.type === 'sms' && config.numbers && !config.phone_numbers) config.phone_numbers = config.numbers
    setForm({
      name: channel.name || '',
      type: channel.type || 'email',
      enabled: channel.enabled ?? true,
      config,
    })
  }, [channel, open])

  const availableGateways = gateways.filter((gateway) => {
    if (form.type === 'email') return gateway.type === 'smtp'
    if (form.type === 'sms') return gateway.type === 'sms'
    return false
  })

  const save = useMutation({
    mutationFn: async () => {
      const config = normalizeConfig(form)
      const payload = { name: form.name.trim(), type: form.type, enabled: form.enabled, config }
      if (isEdit) return (await api.put(`/settings/channels/${channel.id}`, payload)).data
      return (await api.post('/settings/channels', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Channel updated' : 'Channel created')
      qc.invalidateQueries({ queryKey: ['settings', 'channels'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function updateConfig(key: string, value: any) {
    setForm((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))
  }

  function changeType(type: ChannelType) {
    setForm((prev) => ({
      name: prev.name || defaultName(type),
      enabled: prev.enabled,
      type,
      config: {},
    }))
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit channel' : 'New channel'}</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate()
          }}
        >
          <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
            {(Object.keys(channelMeta) as ChannelType[]).map((type) => {
              const meta = channelMeta[type]
              const Icon = meta.icon
              const active = form.type === type
              return (
                <button
                  key={type}
                  type="button"
                  onClick={() => changeType(type)}
                  className={cn(
                    'flex flex-col items-center gap-2 rounded-lg border p-3 text-center transition-colors',
                    active ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-bg text-muted hover:border-primary/40 hover:text-text',
                  )}
                >
                  <Icon className="h-4 w-4" />
                  <span className="text-xs font-medium">{meta.label}</span>
                </button>
              )
            })}
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_auto] md:items-end">
            <FormField label="Channel name" required>
              <Input
                required
                value={form.name}
                onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
                placeholder={defaultName(form.type)}
              />
            </FormField>
            <div className="flex h-9 items-center justify-between gap-3 rounded-md border border-border px-3">
              <span className="text-xs font-medium text-muted">Enabled</span>
              <Switch checked={form.enabled} onCheckedChange={(enabled) => setForm((prev) => ({ ...prev, enabled }))} />
            </div>
          </div>

          {(form.type === 'email' || form.type === 'sms') && (
            <FormField label={`${channelMeta[form.type].gatewayLabel} gateway`}>
              <Select
                value={form.config.gateway_id || '__default'}
                onValueChange={(value) => updateConfig('gateway_id', value === '__default' ? '' : value)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__default">Default gateway</SelectItem>
                  {availableGateways.map((gateway) => (
                    <SelectItem key={gateway.id} value={gateway.id}>
                      {gateway.name || `${gateway.type.toUpperCase()} gateway`}{gateway.is_default ? ' (default)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
          )}

          {form.type === 'email' && (
            <FormField label="Recipients" hint="Comma separated email addresses" required>
              <Input
                required
                value={form.config.recipients || ''}
                onChange={(event) => updateConfig('recipients', event.target.value)}
                placeholder="noc@example.com, oncall@example.com"
              />
            </FormField>
          )}

          {form.type === 'sms' && (
            <FormField label="Phone numbers" hint="Comma separated E.164 numbers" required>
              <Input
                required
                value={form.config.phone_numbers || ''}
                onChange={(event) => updateConfig('phone_numbers', event.target.value)}
                placeholder="+15551234567, +15557654321"
              />
            </FormField>
          )}

          {form.type === 'webhook' && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_160px]">
              <FormField label="Webhook URL" required>
                <Input
                  required
                  value={form.config.url || ''}
                  onChange={(event) => updateConfig('url', event.target.value)}
                  placeholder="https://hooks.example.com/zenplus"
                />
              </FormField>
              <FormField label="Method">
                <Select value={form.config.method || 'POST'} onValueChange={(value) => updateConfig('method', value)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="POST">POST</SelectItem>
                    <SelectItem value="PUT">PUT</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            </div>
          )}

          {form.type === 'slack' && (
            <FormField label="Slack webhook URL" required>
              <Input
                required
                type="password"
                value={form.config.webhook_url || ''}
                onChange={(event) => updateConfig('webhook_url', event.target.value)}
                placeholder="https://hooks.slack.com/services/..."
              />
            </FormField>
          )}

          {form.type === 'telegram' && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="Bot token" required>
                <Input
                  required
                  type="password"
                  value={form.config.bot_token || ''}
                  onChange={(event) => updateConfig('bot_token', event.target.value)}
                />
              </FormField>
              <FormField label="Chat ID" required>
                <Input
                  required
                  value={form.config.chat_id || ''}
                  onChange={(event) => updateConfig('chat_id', event.target.value)}
                  placeholder="-1001234567890"
                />
              </FormField>
            </div>
          )}

          <div className="rounded-lg border border-border bg-bg p-3">
            <div className="flex flex-wrap items-center gap-2">
              <PreviewChip icon={BellRing} label="Alerts" />
              <PreviewChip icon={FileText} label="Reports" />
              <PreviewChip icon={Workflow} label="Escalation" />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || !form.name.trim()}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              {isEdit ? 'Save channel' : 'Create channel'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function MetricCard({
  icon: Icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string | number
  sub: string
  tone: 'success' | 'warning' | 'info' | 'neutral'
}) {
  const toneClass = {
    success: 'bg-success/10 text-success',
    warning: 'bg-warning/10 text-warning',
    info: 'bg-info/10 text-info',
    neutral: 'bg-surface2 text-muted',
  }[tone]
  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <div>
          <div className="text-xs font-medium uppercase text-muted">{label}</div>
          <div className="mt-1 text-2xl font-semibold tracking-tight">{value}</div>
          <div className="mt-1 text-xs text-muted">{sub}</div>
        </div>
        <div className={cn('flex h-10 w-10 shrink-0 items-center justify-center rounded-lg', toneClass)}>
          <Icon className="h-5 w-5" />
        </div>
      </CardContent>
    </Card>
  )
}

function HealthPill({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={cn(
      'inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-medium',
      ok ? 'border-success/30 bg-success/10 text-success' : 'border-warning/30 bg-warning/10 text-warning',
    )}>
      {ok ? <CheckCircle2 className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
      {label}
    </span>
  )
}

function RoutingLane({
  icon: Icon,
  label,
  status,
  ready,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  status: string
  ready: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="h-4 w-4 shrink-0 text-muted" />
        <span className="text-sm font-medium">{label}</span>
      </div>
      <Badge variant={ready ? 'success' : 'outline'}>{status}</Badge>
    </div>
  )
}

function PreviewChip({
  icon: Icon,
  label,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
}) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-text2">
      <Icon className="h-3.5 w-3.5 text-muted" />
      {label}
    </span>
  )
}

function destinationLabel(channel: Channel) {
  const config = channel.config || {}
  switch (channel.type) {
    case 'email':
      return config.recipients || 'No recipients'
    case 'sms':
      return config.phone_numbers || config.numbers || 'No phone numbers'
    case 'webhook':
      return safeUrlLabel(config.url)
    case 'slack':
      return safeUrlLabel(config.webhook_url)
    case 'telegram':
      return config.chat_id ? `Chat ${config.chat_id}` : 'No chat ID'
    default:
      return 'Destination not set'
  }
}

function secondaryDestinationLabel(channel: Channel) {
  const config = channel.config || {}
  switch (channel.type) {
    case 'email':
      return `${recipientCount(config.recipients)} recipient${recipientCount(config.recipients) === 1 ? '' : 's'}`
    case 'sms':
      return `${recipientCount(config.phone_numbers || config.numbers)} number${recipientCount(config.phone_numbers || config.numbers) === 1 ? '' : 's'}`
    case 'webhook':
      return config.method || 'POST'
    case 'slack':
      return 'Incoming webhook'
    case 'telegram':
      return config.bot_token ? 'Bot token configured' : 'Bot token missing'
    default:
      return ''
  }
}

function gatewayLabel(channel: Channel, gateways: Gateway[]) {
  const gatewayId = channel.gateway_id || channel.config?.gateway_id
  if (!gatewayId) return channel.type === 'email' || channel.type === 'sms' ? 'Default gateway' : 'Direct'
  return gateways.find((gateway) => gateway.id === gatewayId)?.name || 'Linked gateway'
}

function safeUrlLabel(value?: string) {
  if (!value) return 'URL not set'
  try {
    const url = new URL(value)
    return `${url.protocol}//${url.host}`
  } catch {
    return value
  }
}

function recipientCount(value?: string) {
  if (!value) return 0
  return value.split(',').map((item) => item.trim()).filter(Boolean).length
}

function defaultName(type: ChannelType) {
  return {
    email: 'Email Operations',
    sms: 'SMS On-call',
    webhook: 'Automation Webhook',
    slack: 'Slack Operations',
    telegram: 'Telegram Operations',
  }[type]
}

function normalizeConfig(form: ChannelForm) {
  const config: Record<string, any> = { ...form.config }
  if (!config.gateway_id) delete config.gateway_id
  if (form.type === 'sms') {
    config.phone_numbers = config.phone_numbers || config.numbers || ''
    delete config.numbers
  }
  if (form.type === 'webhook') {
    config.method = config.method || 'POST'
  }
  return config
}
