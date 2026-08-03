import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  BellRing,
  Check,
  CheckCircle2,
  Clock,
  Filter,
  Info,
  Search,
  ShieldAlert,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, formatDuration, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import {
  AlertChannel, ChannelIcons, cleanAlertMessage, SnoozeMenu,
} from '@/components/alerts/AlertBits'

type AlertStatus = 'active' | 'acknowledged' | 'resolved'
type AlertSeverity = 'critical' | 'warning' | 'info'

type AlertRow = {
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
  metadata?: Record<string, unknown>
  /** Channels the owning rule notifies (resolved server-side). */
  channels?: AlertChannel[]
  /** True when this alert's condition has an active silence. */
  snoozed?: boolean
  snoozed_until?: string | null
}

const STATUS_OPTIONS: AlertStatus[] = ['active', 'acknowledged', 'resolved']
const SEVERITY_OPTIONS: AlertSeverity[] = ['critical', 'warning', 'info']

const RANGE_OPTIONS = [
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 168 },
  { key: '30d', label: '30d', hours: 720 },
  { key: 'all', label: 'All', hours: null },
]

export function AlertsPage() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const urlStatus = searchParams.get('status')
  const status: AlertStatus = urlStatus === 'acknowledged' || urlStatus === 'resolved' ? urlStatus : 'active'
  const severity = searchParams.get('severity') as AlertSeverity | null
  const serviceCheckId = searchParams.get('service_check_id')
  const deviceId = searchParams.get('device_id')
  const rangeKey = searchParams.get('range') || '24h'
  const [search, setSearch] = useState(searchParams.get('q') || '')

  useEffect(() => {
    const id = window.setTimeout(() => {
      const current = searchParams.get('q') || ''
      if (search !== current) patchParams({ q: search || null })
    }, 250)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  const selectedRange = RANGE_OPTIONS.find((r) => r.key === rangeKey) || RANGE_OPTIONS[0]
  const timeBounds = useMemo(() => {
    if (!selectedRange.hours) return {}
    const to = new Date()
    const from = new Date(to.getTime() - selectedRange.hours * 3600_000)
    return { from: from.toISOString(), to: to.toISOString() }
  }, [selectedRange.hours])

  function patchParams(patch: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(patch)) {
      if (!value) next.delete(key)
      else next.set(key, value)
    }
    setSearchParams(next, { replace: true })
  }

  const { data: stats } = useQuery<any>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })

  const qs = useMemo(() => {
    const params = new URLSearchParams({ status, limit: '200' })
    if (severity) params.set('severity', severity)
    if (serviceCheckId) params.set('service_check_id', serviceCheckId)
    if (deviceId) params.set('device_id', deviceId)
    if (search.trim()) params.set('search', search.trim())
    if (timeBounds.from && status !== 'active') params.set('from', timeBounds.from)
    if (timeBounds.to && status !== 'active') params.set('to', timeBounds.to)
    return params.toString()
  }, [status, severity, serviceCheckId, deviceId, search, timeBounds.from, timeBounds.to])

  const { data: alerts = [], isFetching, error } = useQuery<AlertRow[]>({
    queryKey: ['alerts', 'list', qs],
    queryFn: async () => {
      const r = (await api.get(`/alerts?${qs}`)).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  })

  const ack = useMutation({
    mutationFn: async (id: string) => api.post(`/alerts/${id}/acknowledge`),
    onSuccess: () => {
      toast.success('Alert acknowledged')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Acknowledge failed', apiErrorMessage(e)),
  })

  const resolve = useMutation({
    mutationFn: async (id: string) => api.post(`/alerts/${id}/resolve`),
    onSuccess: () => {
      toast.success('Alert resolved')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Resolve failed', apiErrorMessage(e)),
  })

  const activeCritical = alerts.filter((a) => a.status === 'active' && a.severity === 'critical').length
  const mttrSeconds = useMemo(() => {
    const resolved = alerts
      .filter((a) => a.resolved_at)
      .map((a) => (Date.parse(a.resolved_at!) - Date.parse(a.triggered_at)) / 1000)
      .filter((v) => Number.isFinite(v) && v >= 0)
    if (!resolved.length) return null
    return resolved.reduce((a, b) => a + b, 0) / resolved.length
  }, [alerts])

  const clearFilters = () => {
    setSearch('')
    patchParams({ severity: null, service_check_id: null, device_id: null, q: null, range: null })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <ShieldAlert className="h-5 w-5 text-warning" />
            Alert Center
          </h1>
          <p className="text-xs text-muted">
            Triage active incidents, inspect lifecycle context, and prepare routing to notification channels.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline">
            <Link to="/alert-rules">
              <BellRing className="h-4 w-4" />
              Alert rules
            </Link>
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="Active" value={stats?.active ?? 0} icon={AlertTriangle} tone={(stats?.active ?? 0) ? 'danger' : 'success'} />
        <StatCard label="Critical" value={stats?.critical ?? 0} icon={AlertCircle} tone={(stats?.critical ?? 0) ? 'danger' : 'default'} />
        <StatCard label="Warning" value={stats?.warning ?? 0} icon={Info} tone={(stats?.warning ?? 0) ? 'warning' : 'default'} />
        <StatCard label="Acknowledged" value={stats?.acknowledged ?? 0} icon={CheckCircle2} tone="default" />
        <StatCard label="MTTR" value={mttrSeconds == null ? '—' : formatDuration(mttrSeconds)} icon={Clock} tone="default" />
      </div>

      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
                {STATUS_OPTIONS.map((item) => (
                  <button
                    key={item}
                    onClick={() => patchParams({ status: item })}
                    className={`rounded px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
                      status === item ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'
                    }`}
                  >
                    {item}
                  </button>
                ))}
              </div>
              <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
                <button
                  onClick={() => patchParams({ severity: null })}
                  className={`rounded px-2.5 py-1 text-xs font-medium ${!severity ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'}`}
                >
                  All severity
                </button>
                {SEVERITY_OPTIONS.map((item) => (
                  <button
                    key={item}
                    onClick={() => patchParams({ severity: severity === item ? null : item })}
                    className={`rounded px-2.5 py-1 text-xs font-medium capitalize ${severity === item ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'}`}
                  >
                    {item}
                  </button>
                ))}
              </div>
              {status !== 'active' && (
                <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
                  {RANGE_OPTIONS.map((item) => (
                    <button
                      key={item.key}
                      onClick={() => patchParams({ range: item.key === '24h' ? null : item.key })}
                      className={`rounded px-2.5 py-1 text-xs font-medium ${selectedRange.key === item.key ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'}`}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="relative min-w-[240px] xl:w-80">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search alerts, devices, messages..."
                className="h-9 w-full rounded-md border border-border bg-bg pl-8 pr-3 text-xs focus:outline-none focus:ring-1 focus:ring-primary/40"
              />
            </div>
          </div>

          {(severity || serviceCheckId || deviceId || search) && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Filter className="h-3.5 w-3.5 text-muted" />
              {severity && <Badge variant={severityVariant(severity)}>Severity: {severity}</Badge>}
              {deviceId && <Badge variant="info">Device scoped</Badge>}
              {serviceCheckId && <Badge variant="info">Service scoped</Badge>}
              {search && <Badge variant="outline">Search: {search}</Badge>}
              <button onClick={clearFilters} className="text-xs text-muted underline hover:text-text">Clear filters</button>
            </div>
          )}

          {error ? (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
              Failed to load alerts: {apiErrorMessage(error)}
            </div>
          ) : (
            <div className="overflow-hidden rounded-md border border-border">
              <Table>
                <THead className="bg-surface2/50">
                  <Tr>
                    <Th>Alert</Th>
                    <Th>Entity</Th>
                    <Th>Status</Th>
                    <Th title="Notification channels attached to this alert's rule">Notify</Th>
                    <Th>Triggered</Th>
                    <Th className="text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {alerts.map((alert) => (
                    <Tr key={alert.id}>
                      <Td>
                        <div className="flex min-w-0 items-start gap-2">
                          <SeverityIcon severity={alert.severity} />
                          <div className="min-w-0">
                            <Link to={`/alerts/${alert.id}`} className="block max-w-[520px] truncate text-sm font-medium hover:text-primary hover:underline">
                              {cleanAlertMessage(alert.message)}
                            </Link>
                            <div className="mt-1 flex flex-wrap items-center gap-1.5">
                              <Badge variant={severityVariant(alert.severity)}>{alert.severity}</Badge>
                              {alert.service_check_name && <Badge variant="outline">service</Badge>}
                              {alert.metadata?.is_recovery === true && <Badge variant="success">recovery</Badge>}
                              {alert.snoozed && (
                                <Badge variant="outline" title={alert.snoozed_until ? `Snoozed until ${new Date(alert.snoozed_until).toLocaleString()}` : 'Muted until cleared'}>
                                  snoozed
                                </Badge>
                              )}
                            </div>
                          </div>
                        </div>
                      </Td>
                      <Td>
                        {alert.device_id ? (
                          <Link to={`/devices/${alert.device_id}`} className="text-sm font-medium hover:text-primary hover:underline">
                            {alert.device_hostname || alert.device_ip || 'Device'}
                            {alert.device_ip && <div className="font-mono text-[11px] font-normal text-muted">{alert.device_ip}</div>}
                          </Link>
                        ) : alert.service_check_id ? (
                          <Link to={`/services/${alert.service_check_id}`} className="text-sm font-medium hover:text-primary hover:underline">
                            {alert.service_check_name || 'Service check'}
                          </Link>
                        ) : (
                          <span className="text-sm text-muted">System</span>
                        )}
                      </Td>
                      <Td><StatusBadge status={alert.status} /></Td>
                      <Td><ChannelIcons channels={alert.channels} /></Td>
                      <Td className="text-xs text-muted">
                        <div>{relativeTime(alert.triggered_at)}</div>
                        <div className="font-mono text-[10px]">{formatDateTime(alert.triggered_at)}</div>
                      </Td>
                      <Td>
                        <div className="flex justify-end gap-1">
                          <Button asChild size="sm" variant="outline">
                            <Link to={`/alerts/${alert.id}`}>Open <ArrowRight className="h-3.5 w-3.5" /></Link>
                          </Button>
                          {alert.status === 'active' && (
                            <Button size="sm" variant="outline" onClick={() => ack.mutate(alert.id)} disabled={ack.isPending}>
                              <Check className="h-3.5 w-3.5" /> Ack
                            </Button>
                          )}
                          {(alert.status === 'active' || alert.status === 'acknowledged') && (
                            <Button size="sm" variant="outline" onClick={() => resolve.mutate(alert.id)} disabled={resolve.isPending}>
                              <X className="h-3.5 w-3.5" /> Resolve
                            </Button>
                          )}
                          {/* Snoozable = the condition has an identity the evaluators
                              can match: device+rule, or a server dedupe key. */}
                          {(alert.status === 'active' || alert.status === 'acknowledged' || alert.snoozed) &&
                            ((alert.device_id && alert.rule_id) || alert.metadata?.dedupe != null) && (
                            <SnoozeMenu alertId={alert.id} snoozed={alert.snoozed} />
                          )}
                        </div>
                      </Td>
                    </Tr>
                  ))}
                  {alerts.length === 0 && (
                    <Tr>
                      <Td colSpan={6} className="py-12 text-center text-muted">
                        {isFetching ? 'Loading alerts...' : `No ${status} alerts`}
                      </Td>
                    </Tr>
                  )}
                </TBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {activeCritical > 0 && (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
          {activeCritical} critical active alert{activeCritical === 1 ? '' : 's'} need immediate triage before lower-priority items.
        </div>
      )}
    </div>
  )
}

function StatCard({ label, value, icon: Icon, tone }: { label: string; value: number | string; icon: React.ComponentType<{ className?: string }>; tone: 'danger' | 'warning' | 'success' | 'default' }) {
  const color = tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : tone === 'success' ? 'text-success' : 'text-text'
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-10 w-10 items-center justify-center rounded-md ${tone === 'danger' ? 'bg-danger/10' : tone === 'warning' ? 'bg-warning/10' : tone === 'success' ? 'bg-success/10' : 'bg-surface2'}`}>
          <Icon className={`h-4 w-4 ${color}`} />
        </div>
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className={`text-xl font-semibold tabular-nums ${color}`}>{value}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function SeverityIcon({ severity }: { severity: AlertSeverity }) {
  const Icon = severity === 'critical' ? AlertCircle : severity === 'warning' ? AlertTriangle : Info
  const cls = severity === 'critical' ? 'bg-danger/10 text-danger' : severity === 'warning' ? 'bg-warning/10 text-warning' : 'bg-info/10 text-info'
  return <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${cls}`}><Icon className="h-4 w-4" /></span>
}

function severityVariant(severity: AlertSeverity): 'danger' | 'warning' | 'info' {
  return severity === 'critical' ? 'danger' : severity === 'warning' ? 'warning' : 'info'
}

function StatusBadge({ status }: { status: AlertStatus }) {
  const variant = status === 'resolved' ? 'success' : status === 'acknowledged' ? 'warning' : 'danger'
  return <Badge variant={variant}>{status}</Badge>
}

function formatDateTime(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
