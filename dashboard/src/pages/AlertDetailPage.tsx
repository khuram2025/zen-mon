import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Check,
  CheckCircle2,
  Clock,
  Info,
  Route,
  Server,
  ShieldAlert,
  Wrench,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, formatDuration, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'

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
  rule?: {
    id: string
    name: string
    metric: string
    operator: string
    threshold: number
    duration: number
    cooldown: number
    enabled: boolean
  } | null
  entity?: { kind: string; id: string | null; name: string }
  related_alerts?: AlertDetail[]
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
          </h1>
          <p className="max-w-4xl text-sm text-muted">{alert.message}</p>
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
                      <div className="truncate text-xs font-medium">{item.message}</div>
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

          <Card>
            <CardContent className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Rule / Policy</h2>
              {alert.rule ? (
                <div className="space-y-2 text-xs">
                  <InfoRow label="Rule" value={alert.rule.name} />
                  <InfoRow label="Condition" value={`${alert.rule.metric} ${alert.rule.operator} ${alert.rule.threshold}`} />
                  <InfoRow label="Duration" value={alert.rule.duration ? formatDuration(alert.rule.duration) : 'Immediate'} />
                  <InfoRow label="Cooldown" value={formatDuration(alert.rule.cooldown)} />
                </div>
              ) : (
                <div className="text-xs text-muted">No rule metadata linked.</div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-4">
              <h2 className="mb-2 text-sm font-semibold">Notification Channels</h2>
              <div className="rounded-md border border-dashed border-border bg-surface2/30 p-3 text-xs text-muted">
                Channel routing will attach here later: email, SMS, webhooks, and escalation policies.
              </div>
            </CardContent>
          </Card>

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

