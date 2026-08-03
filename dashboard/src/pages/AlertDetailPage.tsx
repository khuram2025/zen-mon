import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Check,
  CheckCircle2,
  Clock,
  Eye,
  Info,
  Loader2,
  Pencil,
  Power,
  Route,
  Server,
  ShieldAlert,
  Trash2,
  Wrench,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, formatDuration, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import {
  AlertChannel, ChannelIcons, cleanAlertMessage, SnoozeMenu,
} from '@/components/alerts/AlertBits'

type AlertStatus = 'active' | 'acknowledged' | 'resolved'
type AlertSeverity = 'critical' | 'warning' | 'info'

type AlertDetail = {
  id: string
  rule_id: string | null
  device_id: string | null
  device_hostname: string | null
  device_ip: string | null
  service_check_id: string | null
  service_check_name: string | null
  status: AlertStatus
  severity: AlertSeverity
  message: string
  triggered_at: string
  acknowledged_at: string | null
  resolved_at: string | null
  metadata?: Record<string, any>
  rule?: AlertRuleInfo | null
  entity?: { kind: string; id: string | null; name: string }
  related_alerts?: AlertDetail[]
  channels?: AlertChannel[]
  snoozed?: boolean
  snoozed_until?: string | null
}

type AlertRuleInfo = {
  id: string
  name: string
  metric: string
  operator: string
  threshold: number | null
  duration: number
  cooldown: number
  enabled: boolean
  severity?: string
  target?: string | null
  min_duration?: number
  notify_channels?: string[]
  trigger_on?: string
  recovery_alert?: boolean
  conditions?: { metric: string; operator: string; threshold: number }[] | null
}

export function AlertDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: alert, isLoading, error } = useQuery<AlertDetail>({
    queryKey: ['alerts', 'detail', id],
    queryFn: async () => (await api.get(`/alerts/${id}`)).data,
    enabled: !!id,
    refetchInterval: 15_000,
  })

  const ack = useMutation({
    mutationFn: async () => api.post(`/alerts/${id}/acknowledge`),
    onSuccess: () => {
      toast.success('Alert acknowledged')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Acknowledge failed', apiErrorMessage(e)),
  })

  const resolve = useMutation({
    mutationFn: async () => api.post(`/alerts/${id}/resolve`),
    onSuccess: () => {
      toast.success('Alert resolved')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Resolve failed', apiErrorMessage(e)),
  })

  if (isLoading) return <div className="py-12 text-center text-sm text-muted">Loading alert...</div>
  if (error || !alert) {
    return (
      <Card>
        <CardContent className="p-6 text-sm text-danger">
          Failed to load alert: {apiErrorMessage(error)}
        </CardContent>
      </Card>
    )
  }

  const duration = alert.resolved_at
    ? formatDuration(Math.max(0, (Date.parse(alert.resolved_at) - Date.parse(alert.triggered_at)) / 1000))
    : formatDuration(Math.max(0, (Date.now() - Date.parse(alert.triggered_at)) / 1000))

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <button onClick={() => navigate(-1)} className="mb-2 inline-flex items-center gap-1 text-xs text-muted hover:text-text">
            <ArrowLeft className="h-3.5 w-3.5" />
            Back
          </button>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <SeverityIcon severity={alert.severity} compact />
            Alert Detail
            {alert.snoozed && (
              <Badge variant="outline" title={alert.snoozed_until ? `Snoozed until ${new Date(alert.snoozed_until).toLocaleString()}` : 'Muted until cleared'}>
                snoozed
              </Badge>
            )}
          </h1>
          <p className="max-w-4xl text-sm text-muted">{cleanAlertMessage(alert.message)}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {alert.status === 'active' && (
            <Button variant="outline" onClick={() => ack.mutate()} disabled={ack.isPending}>
              <Check className="h-4 w-4" />
              Acknowledge
            </Button>
          )}
          {(alert.status === 'active' || alert.status === 'acknowledged') && (
            <Button variant="outline" onClick={() => resolve.mutate()} disabled={resolve.isPending}>
              <X className="h-4 w-4" />
              Resolve
            </Button>
          )}
          {((alert.device_id && alert.rule_id) || alert.metadata?.dedupe != null) && (
            <SnoozeMenu alertId={alert.id} snoozed={alert.snoozed} size="default" />
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Summary label="Severity" value={alert.severity} icon={ShieldAlert} tone={alert.severity} />
        <Summary label="Status" value={alert.status} icon={CheckCircle2} tone={alert.status === 'resolved' ? 'success' : alert.status === 'acknowledged' ? 'warning' : 'critical'} />
        <Summary label={alert.resolved_at ? 'Time to Resolve' : 'Open For'} value={duration} icon={Clock} tone="info" />
        <Summary label="Source" value={alert.entity?.kind || 'system'} icon={Route} tone="info" />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4">
          <Card>
            <CardContent className="p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Investigation Context</h2>
                <StatusBadge status={alert.status} />
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <InfoRow label="Triggered" value={`${formatDateTime(alert.triggered_at)} (${relativeTime(alert.triggered_at)})`} />
                <InfoRow label="Acknowledged" value={alert.acknowledged_at ? `${formatDateTime(alert.acknowledged_at)} (${relativeTime(alert.acknowledged_at)})` : 'Not acknowledged'} />
                <InfoRow label="Resolved" value={alert.resolved_at ? `${formatDateTime(alert.resolved_at)} (${relativeTime(alert.resolved_at)})` : 'Not resolved'} />
                <InfoRow label="Alert ID" value={alert.id} mono />
              </div>
              <div className="mt-4 rounded-md border border-border bg-surface2/30 p-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted">Lifecycle Timeline</div>
                <div className="space-y-2 text-xs">
                  <TimelineRow icon={AlertTriangle} label="Triggered" at={alert.triggered_at} />
                  {alert.acknowledged_at && <TimelineRow icon={Check} label="Acknowledged" at={alert.acknowledged_at} />}
                  {alert.resolved_at && <TimelineRow icon={CheckCircle2} label="Resolved" at={alert.resolved_at} />}
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Related Alerts</h2>
              <div className="space-y-2">
                {alert.related_alerts?.length ? alert.related_alerts.map((item) => (
                  <Link
                    key={item.id}
                    to={`/alerts/${item.id}`}
                    className="flex items-start gap-2 rounded-md border border-border bg-surface2/30 p-2 transition-colors hover:border-primary/50 hover:bg-primary/5"
                  >
                    <SeverityIcon severity={item.severity} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium">{cleanAlertMessage(item.message)}</div>
                      <div className="text-[10px] text-muted">{relativeTime(item.triggered_at)} · {item.status}</div>
                    </div>
                  </Link>
                )) : (
                  <div className="rounded-md border border-dashed border-border p-5 text-center text-xs text-muted">
                    No related alerts found for this entity.
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardContent className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Affected Entity</h2>
              {alert.device_id ? (
                <EntityLink to={`/devices/${alert.device_id}`} icon={Server} title={alert.device_hostname || 'Device'} subtitle={alert.device_ip || 'device'} />
              ) : alert.service_check_id ? (
                <EntityLink to={`/services/${alert.service_check_id}`} icon={ActivityIcon} title={alert.service_check_name || 'Service check'} subtitle="service check" />
              ) : (
                <div className="text-sm text-muted">System-level alert</div>
              )}
            </CardContent>
          </Card>

          <RuleCard rule={alert.rule ?? null} />

          <ChannelsCard rule={alert.rule ?? null} channels={alert.channels} />

          {alert.rule && <MessagePreviewCard ruleId={alert.rule.id} />}

          <Card>
            <CardContent className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Raw Metadata</h2>
              <pre className="max-h-64 overflow-auto rounded-md bg-bg p-3 text-[10px] text-muted">
                {JSON.stringify(alert.metadata || {}, null, 2)}
              </pre>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

function Summary({ label, value, icon: Icon, tone }: { label: string; value: string; icon: React.ComponentType<{ className?: string }>; tone: AlertSeverity | 'success' | 'info' }) {
  const color = tone === 'critical' ? 'text-danger bg-danger/10' : tone === 'warning' ? 'text-warning bg-warning/10' : tone === 'success' ? 'text-success bg-success/10' : 'text-info bg-info/10'
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <span className={`flex h-10 w-10 items-center justify-center rounded-md ${color}`}>
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className="truncate text-lg font-semibold capitalize">{value}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function EntityLink({ to, icon: Icon, title, subtitle }: { to: string; icon: React.ComponentType<{ className?: string }>; title: string; subtitle: string }) {
  return (
    <Link to={to} className="flex items-center gap-3 rounded-md border border-border bg-surface2/30 p-3 hover:border-primary/50 hover:bg-primary/5">
      <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{title}</span>
        <span className="block truncate text-xs text-muted">{subtitle}</span>
      </span>
    </Link>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 text-sm ${mono ? 'font-mono text-xs' : ''}`}>{value}</div>
    </div>
  )
}

function TimelineRow({ icon: Icon, label, at }: { icon: React.ComponentType<{ className?: string }>; label: string; at: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary/10 text-primary">
        <Icon className="h-3 w-3" />
      </span>
      <span className="font-medium">{label}</span>
      <span className="ml-auto font-mono text-[10px] text-muted">{formatDateTime(at)}</span>
    </div>
  )
}

function SeverityIcon({ severity, compact = false }: { severity: AlertSeverity; compact?: boolean }) {
  const Icon = severity === 'critical' ? AlertCircle : severity === 'warning' ? AlertTriangle : Info
  const cls = severity === 'critical' ? 'bg-danger/10 text-danger' : severity === 'warning' ? 'bg-warning/10 text-warning' : 'bg-info/10 text-info'
  return <span className={`flex shrink-0 items-center justify-center rounded-md ${compact ? 'h-8 w-8' : 'h-7 w-7'} ${cls}`}><Icon className="h-4 w-4" /></span>
}

function StatusBadge({ status }: { status: AlertStatus }) {
  const variant = status === 'resolved' ? 'success' : status === 'acknowledged' ? 'warning' : 'danger'
  return <Badge variant={variant}>{status}</Badge>
}

function formatDateTime(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

const ActivityIcon = Wrench

/* ════════════════════════════════════════════════════════════
   Rule / Policy — full CRUD on the owning alert rule
   ════════════════════════════════════════════════════════════ */

function RuleCard({ rule }: { rule: AlertRuleInfo | null }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editOpen, setEditOpen] = useState(false)
  const compound = (rule?.conditions?.length || 0) > 1

  const toggle = useMutation({
    mutationFn: async () => (await api.post(`/alert-rules/${rule!.id}/toggle`)).data,
    onSuccess: (d: any) => {
      toast.success(d?.enabled ? 'Rule enabled' : 'Rule disabled')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })

  const remove = useMutation({
    mutationFn: async () => api.delete(`/alert-rules/${rule!.id}`),
    onSuccess: () => {
      toast.success('Alert rule deleted — no new alerts will be raised by it')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      navigate('/alert-rules')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Rule / Policy</h2>
          {rule && (
            <div className="flex items-center gap-0.5">
              <Button
                size="sm" variant="ghost" className="h-7 w-7 p-0"
                title={rule.enabled ? 'Disable rule' : 'Enable rule'}
                disabled={toggle.isPending}
                onClick={() => toggle.mutate()}
              >
                <Power className={`h-3.5 w-3.5 ${rule.enabled ? 'text-success' : 'text-muted'}`} />
              </Button>
              {compound ? (
                <Button size="sm" variant="ghost" className="h-7 w-7 p-0" title="Compound rule — edit in Alert Rules" asChild>
                  <Link to="/alert-rules"><Pencil className="h-3.5 w-3.5" /></Link>
                </Button>
              ) : (
                <Button size="sm" variant="ghost" className="h-7 w-7 p-0" title="Edit rule" onClick={() => setEditOpen(true)}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
              )}
              <Button
                size="sm" variant="ghost" className="h-7 w-7 p-0" title="Delete rule"
                disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm(`Delete alert rule "${rule.name}"? Existing alerts stay in history; no new ones will fire.`)) remove.mutate()
                }}
              >
                <Trash2 className="h-3.5 w-3.5 text-danger" />
              </Button>
            </div>
          )}
        </div>
        {rule ? (
          <div className="space-y-2 text-xs">
            <InfoRow label="Rule" value={rule.name} />
            <InfoRow
              label="Condition"
              value={compound
                ? `${rule.conditions!.length} conditions`
                : `${rule.metric} ${rule.operator} ${rule.threshold ?? '—'}`}
            />
            <InfoRow label="Severity" value={rule.severity || 'warning'} />
            <InfoRow label="Scope" value={rule.target ? `interface “${rule.target}”` : 'device-wide'} />
            <InfoRow label="Sustained" value={rule.min_duration ? formatDuration(rule.min_duration) : 'Immediate'} />
            <InfoRow label="Cooldown" value={formatDuration(rule.cooldown)} />
            <InfoRow label="State" value={rule.enabled ? 'Enabled' : 'Disabled'} />
          </div>
        ) : (
          <div className="text-xs text-muted">No rule metadata linked.</div>
        )}
      </CardContent>
      {rule && (
        <RuleEditDialog open={editOpen} onOpenChange={setEditOpen} rule={rule} />
      )}
    </Card>
  )
}

function RuleEditDialog({
  open, onOpenChange, rule,
}: { open: boolean; onOpenChange: (v: boolean) => void; rule: AlertRuleInfo }) {
  const qc = useQueryClient()
  const [name, setName] = useState(rule.name)
  const [operator, setOperator] = useState(rule.operator || '>')
  const [threshold, setThreshold] = useState(String(rule.threshold ?? ''))
  const [severity, setSeverity] = useState(rule.severity || 'warning')
  const [minutes, setMinutes] = useState(String(Math.round((rule.min_duration || 0) / 60)))

  useEffect(() => {
    if (!open) return
    setName(rule.name)
    setOperator(rule.operator || '>')
    setThreshold(String(rule.threshold ?? ''))
    setSeverity(rule.severity || 'warning')
    setMinutes(String(Math.round((rule.min_duration || 0) / 60)))
  }, [open, rule])

  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        name,
        operator,
        threshold: Number(threshold),
        severity,
        min_duration: Math.round(Number(minutes) * 60),
      }
      // Keep a stored single-condition mirror in step with the flat fields.
      if (rule.conditions?.length === 1) {
        body.conditions = [{ metric: rule.metric, operator, threshold: Number(threshold) }]
      }
      return (await api.put(`/alert-rules/${rule.id}`, body)).data
    },
    onSuccess: () => {
      toast.success('Alert rule updated')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-4 w-4 text-primary" /> Edit rule · {rule.name}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div>
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-1">
              <Label>Metric</Label>
              <Input value={rule.metric} disabled className="opacity-70" />
            </div>
            <div>
              <Label>Operator</Label>
              <Select value={operator} onValueChange={setOperator}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {['>', '>=', '<', '<=', '==', '!='].map((op) => (
                    <SelectItem key={op} value={op}>{op}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Threshold</Label>
              <Input type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Severity</Label>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="critical">Critical</SelectItem>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="info">Info</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Sustained (min)</Label>
              <Input type="number" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save changes'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ════════════════════════════════════════════════════════════
   Notification Channels — attached channels + routing editor
   ════════════════════════════════════════════════════════════ */

function ChannelsCard({ rule, channels }: { rule: AlertRuleInfo | null; channels?: AlertChannel[] }) {
  const [editOpen, setEditOpen] = useState(false)
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Notification Channels</h2>
          {rule && (
            <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => setEditOpen(true)}>
              <Pencil className="h-3 w-3" /> Edit
            </Button>
          )}
        </div>
        {!rule ? (
          <div className="text-xs text-muted">No rule linked — routing is configured on the alert rule.</div>
        ) : !channels || channels.length === 0 ? (
          <div className="rounded-md border border-dashed border-warning/40 bg-warning/5 p-3 text-xs text-warning">
            No channels attached — this rule records alerts but notifies nobody.
          </div>
        ) : (
          <div className="space-y-1.5">
            {channels.map((c) => (
              <div key={c.id} className={`flex items-center gap-2 rounded-md border border-border/60 bg-surface2/30 px-2.5 py-1.5 ${c.enabled ? '' : 'opacity-60'}`}>
                <ChannelIcons channels={[c]} />
                <span className="min-w-0 flex-1 truncate text-xs font-medium">{c.name}</span>
                <span className="text-[10px] uppercase tracking-wider text-muted">{c.type}</span>
                {!c.enabled && <Badge variant="outline" className="text-[9px]">disabled</Badge>}
              </div>
            ))}
          </div>
        )}
      </CardContent>
      {rule && (
        <ChannelsEditDialog open={editOpen} onOpenChange={setEditOpen} rule={rule} />
      )}
    </Card>
  )
}

function ChannelsEditDialog({
  open, onOpenChange, rule,
}: { open: boolean; onOpenChange: (v: boolean) => void; rule: AlertRuleInfo }) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set(rule.notify_channels || []))

  useEffect(() => {
    if (open) setSelected(new Set(rule.notify_channels || []))
  }, [open, rule])

  const { data, error, isLoading } = useQuery<{ data: AlertChannel[] }>({
    queryKey: ['channels'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })
  const all = data?.data || []

  const save = useMutation({
    mutationFn: async () =>
      (await api.put(`/alert-rules/${rule.id}`, { notify_channels: Array.from(selected) })).data,
    onSuccess: () => {
      toast.success('Notification routing updated')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Notification routing · {rule.name}</DialogTitle>
        </DialogHeader>
        <div className="space-y-2 text-sm">
          {isLoading && <div className="py-4 text-center text-xs text-muted"><Loader2 className="mx-auto h-4 w-4 animate-spin" /></div>}
          {error != null && (
            <div className="rounded-md border border-danger/30 bg-danger/5 p-3 text-xs text-danger">
              Could not load channels: {apiErrorMessage(error)}. Managing channels requires admin access.
            </div>
          )}
          {!isLoading && !error && all.length === 0 && (
            <div className="text-xs text-muted">
              No notification channels exist yet — create one under{' '}
              <Link to="/settings/notifications" className="text-primary hover:underline" onClick={() => onOpenChange(false)}>Settings → Notifications</Link>.
            </div>
          )}
          {all.map((c) => (
            <label key={c.id} className={`flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 ${selected.has(c.id) ? 'border-primary/50 bg-primary/5' : 'border-border bg-surface2/30'}`}>
              <input
                type="checkbox"
                checked={selected.has(c.id)}
                onChange={(e) => {
                  const next = new Set(selected)
                  if (e.target.checked) next.add(c.id); else next.delete(c.id)
                  setSelected(next)
                }}
              />
              <ChannelIcons channels={[c]} />
              <span className="min-w-0 flex-1 truncate text-xs font-medium">{c.name}</span>
              <span className="text-[10px] uppercase tracking-wider text-muted">{c.type}</span>
              {!c.enabled && <Badge variant="outline" className="text-[9px]">disabled</Badge>}
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !!error}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save routing'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ════════════════════════════════════════════════════════════
   Message preview — what the channels would actually receive
   ════════════════════════════════════════════════════════════ */

function MessagePreviewCard({ ruleId }: { ruleId: string }) {
  const [show, setShow] = useState(false)
  const [tab, setTab] = useState<'email' | 'sms'>('email')

  const { data, isFetching, error } = useQuery<{
    alert: { subject: string; email_body: string; sms_body: string }
    recovery: { subject: string; email_body: string; sms_body: string } | null
  }>({
    queryKey: ['alert-rules', ruleId, 'preview'],
    queryFn: async () => (await api.post(`/alert-rules/${ruleId}/preview`)).data,
    enabled: show,
    staleTime: 60_000,
  })

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Message Preview</h2>
          {show && (
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
              {(['email', 'sms'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase ${tab === t ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'}`}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
        </div>
        {!show ? (
          <Button size="sm" variant="outline" onClick={() => setShow(true)}>
            <Eye className="h-3.5 w-3.5" /> Preview notification
          </Button>
        ) : isFetching ? (
          <div className="py-4 text-center text-xs text-muted"><Loader2 className="mx-auto h-4 w-4 animate-spin" /></div>
        ) : error != null ? (
          <div className="text-xs text-danger">Preview failed: {apiErrorMessage(error)}</div>
        ) : data ? (
          tab === 'email' ? (
            <div className="space-y-2">
              <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
                <span className="mr-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Subject</span>
                {data.alert.subject}
              </div>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 text-[11px] leading-relaxed text-text/90">
                {data.alert.email_body}
              </pre>
              {data.recovery && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-muted hover:text-text">Recovery message</summary>
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 text-[11px] text-text/90">
                    {data.recovery.email_body}
                  </pre>
                </details>
              )}
            </div>
          ) : (
            <div className="rounded-md border border-border bg-surface2/40 p-3 font-mono text-[11px] leading-relaxed">
              {data.alert.sms_body}
            </div>
          )
        ) : null}
        <p className="mt-2 text-[10px] text-muted">
          Rendered with sample values — edit templates on the rule in Alert Rules.
        </p>
      </CardContent>
    </Card>
  )
}

