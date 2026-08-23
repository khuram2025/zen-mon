import { useMemo } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, ArrowLeft, ArrowRight, CheckCircle2, Clock3,
  ExternalLink, Filter, Gauge, HelpCircle, History, ShieldAlert, Timer, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import type { ServiceCheck } from '@/types'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'

type StatusEvent = {
  timestamp: string
  old_status: string | null
  new_status: string
  reason?: string | null
  duration_sec?: number | null
}

const FILTERS = [
  { key: 'all', label: 'All events' },
  { key: 'incidents', label: 'Incidents only' },
  { key: 'down', label: 'Down' },
  { key: 'warning', label: 'Warning' },
] as const

type FilterKey = (typeof FILTERS)[number]['key']

const statusMeta: Record<string, { label: string; Icon: typeof CheckCircle2; tone: 'success' | 'danger' | 'warning' | 'outline'; accent: string }> = {
  up: { label: 'Healthy', Icon: CheckCircle2, tone: 'success', accent: 'bg-success' },
  down: { label: 'Down', Icon: XCircle, tone: 'danger', accent: 'bg-danger' },
  degraded: { label: 'Degraded', Icon: AlertTriangle, tone: 'warning', accent: 'bg-warning' },
  warning: { label: 'Warning', Icon: AlertTriangle, tone: 'warning', accent: 'bg-warning' },
  unknown: { label: 'Unknown', Icon: HelpCircle, tone: 'outline', accent: 'bg-muted' },
}

function formatDur(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return 'Not recorded'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${Math.floor(sec % 60)}s`
  return `${Math.floor(sec)}s`
}

function eventMeta(status: string) {
  return statusMeta[status] || statusMeta.unknown
}

export function ServiceIncidentsPage() {
  const { id = '' } = useParams<{ id: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const filter: FilterKey = FILTERS.find((item) => item.key === searchParams.get('filter'))?.key || 'incidents'

  const { data: check } = useQuery<ServiceCheck>({
    queryKey: ['service-check', id],
    queryFn: async () => (await api.get(`/service-checks/${id}`)).data,
    enabled: !!id,
    refetchInterval: 15_000,
  })

  const { data: history = [], isLoading } = useQuery<StatusEvent[]>({
    queryKey: ['service-status-history-full', id],
    queryFn: async () => (await api.get(`/service-checks/${id}/status-history?limit=500`)).data,
    enabled: !!id,
    refetchInterval: 30_000,
  })

  const filtered = useMemo(() => {
    if (filter === 'all') return history
    if (filter === 'down') return history.filter((item) => item.new_status === 'down')
    if (filter === 'warning') return history.filter((item) => ['warning', 'degraded'].includes(item.new_status))
    return history.filter((item) => item.new_status !== 'up')
  }, [history, filter])

  const stats = useMemo(() => {
    const incidents = history.filter((item) => item.new_status !== 'up')
    const totalDown = incidents.reduce((sum, item) => sum + (item.duration_sec || 0), 0)
    const longest = incidents.reduce((maximum, item) => Math.max(maximum, item.duration_sec || 0), 0)
    return {
      incidents: incidents.length,
      down: history.filter((item) => item.new_status === 'down').length,
      warning: history.filter((item) => ['warning', 'degraded'].includes(item.new_status)).length,
      recoveries: history.filter((item) => item.new_status === 'up').length,
      totalDown,
      longest,
    }
  }, [history])

  const currentMeta = eventMeta(check?.status || 'unknown')
  const CurrentIcon = currentMeta.Icon
  const latest = history[0]

  function setFilter(value: FilterKey) {
    const next = new URLSearchParams(searchParams)
    next.set('filter', value)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="space-y-5 pb-8">
      <div className="flex flex-col gap-4 rounded-2xl border border-border bg-surface p-5 shadow-sm lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-start gap-3">
          <Button asChild variant="ghost" size="icon" className="mt-0.5 h-9 w-9 shrink-0">
            <Link to={`/services/${id}`} aria-label="Back to service"><ArrowLeft className="h-4 w-4" /></Link>
          </Button>
          <div>
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <Badge variant="outline">Monitoring</Badge>
              <span className="text-xs text-muted">/</span>
              <Badge variant={currentMeta.tone}><CurrentIcon className="h-3 w-3" /> {currentMeta.label}</Badge>
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">Incident history</h1>
            <p className="mt-1 text-sm text-muted">
              {check?.name || 'Service'} · Status transitions, recovery events, and recorded downtime.
            </p>
          </div>
        </div>
        <Button asChild variant="outline"><Link to={`/services/${id}`}>Open service overview <ExternalLink className="h-4 w-4" /></Link></Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={ShieldAlert} label="Incidents" value={String(stats.incidents)} detail={`${stats.down} down · ${stats.warning} warning`} tone="danger" />
        <StatCard icon={Timer} label="Recorded downtime" value={formatDur(stats.totalDown)} detail={`Longest: ${formatDur(stats.longest)}`} tone={stats.totalDown > 0 ? 'warning' : 'success'} />
        <StatCard icon={CheckCircle2} label="Recoveries" value={String(stats.recoveries)} detail="Transitions back to healthy" tone="success" />
        <StatCard icon={History} label="Timeline records" value={String(history.length)} detail={latest ? `Latest ${relativeTime(latest.timestamp)}` : 'No events recorded'} tone="info" />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card>
          <CardContent className="p-0">
            <div className="flex flex-col gap-3 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="flex items-center gap-2 text-sm font-semibold"><Activity className="h-4 w-4 text-primary" /> Event timeline</h2>
                <p className="mt-0.5 text-xs text-muted">Showing {filtered.length} of {history.length} recorded transitions</p>
              </div>
              <div className="flex flex-wrap items-center gap-1 rounded-lg border border-border bg-surface2/50 p-1" role="group" aria-label="Incident filter">
                <Filter className="ml-1 mr-0.5 h-3.5 w-3.5 text-muted" />
                {FILTERS.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setFilter(item.key)}
                    className={`rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${filter === item.key ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'}`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>

            {isLoading ? (
              <div className="py-20 text-center text-sm text-muted">Loading incident history…</div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-20 text-center">
                <div className="rounded-full bg-success/10 p-3 text-success"><CheckCircle2 className="h-6 w-6" /></div>
                <div><div className="font-medium">No matching incidents</div><p className="mt-1 text-sm text-muted">There are no events for the selected filter.</p></div>
              </div>
            ) : (
              <div className="divide-y divide-border">
                {filtered.map((event, index) => (
                  <IncidentRow key={`${event.timestamp}-${index}`} event={event} longest={stats.longest} />
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card><CardContent className="p-4">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Current state</div>
            <div className="mt-3 flex items-center gap-3">
              <div className={`rounded-xl p-3 text-white ${currentMeta.accent}`}><CurrentIcon className="h-6 w-6" /></div>
              <div><div className="text-xl font-semibold">{currentMeta.label}</div><div className="text-xs text-muted">Live service status</div></div>
            </div>
            <div className="mt-4 space-y-3 border-t border-border pt-4 text-sm">
              <ContextRow label="Service" value={check?.name || 'Loading…'} />
              <ContextRow label="Last checked" value={check?.last_check_at ? new Date(check.last_check_at).toLocaleString() : 'Not available'} />
              <ContextRow label="Monitoring" value={check?.enabled === false ? 'Paused' : 'Active'} />
              <ContextRow label="Maintenance" value={check?.in_maintenance ? 'In maintenance' : 'None'} />
            </div>
          </CardContent></Card>

          <Card><CardContent className="p-4">
            <div className="flex items-center gap-2 text-sm font-semibold"><Gauge className="h-4 w-4 text-primary" /> Reliability context</div>
            <div className="mt-4 space-y-3">
              <ContextRow label="Longest incident" value={formatDur(stats.longest)} />
              <ContextRow label="Total downtime" value={formatDur(stats.totalDown)} />
              <ContextRow label="Down transitions" value={String(stats.down)} />
              <ContextRow label="Recoveries" value={String(stats.recoveries)} />
            </div>
            <p className="mt-4 rounded-lg bg-surface2/60 p-3 text-xs leading-relaxed text-muted">
              Duration is shown only when the monitoring engine recorded an incident close time. Historical configuration failures remain visible for auditability.
            </p>
          </CardContent></Card>
        </div>
      </div>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, detail, tone }: { icon: typeof Activity; label: string; value: string; detail: string; tone: 'danger' | 'warning' | 'success' | 'info' }) {
  const colors = {
    danger: 'bg-danger/10 text-danger', warning: 'bg-warning/10 text-warning', success: 'bg-success/10 text-success', info: 'bg-info/10 text-info',
  }
  return <Card><CardContent className="flex items-center gap-3 p-4"><div className={`rounded-xl p-2.5 ${colors[tone]}`}><Icon className="h-5 w-5" /></div><div className="min-w-0"><div className="text-xs font-medium text-muted">{label}</div><div className="mt-0.5 text-xl font-semibold tabular-nums">{value}</div><div className="truncate text-[11px] text-muted">{detail}</div></div></CardContent></Card>
}

function IncidentRow({ event, longest }: { event: StatusEvent; longest: number }) {
  const next = eventMeta(event.new_status)
  const previous = event.old_status ? eventMeta(event.old_status) : null
  const Icon = next.Icon
  const isLongest = longest > 0 && event.duration_sec === longest
  return (
    <div className="group grid gap-3 p-4 transition-colors hover:bg-surface2/30 sm:grid-cols-[44px_minmax(0,1fr)_auto]">
      <div className="relative flex justify-center">
        <div className={`relative z-10 flex h-9 w-9 items-center justify-center rounded-full text-white shadow-sm ${next.accent}`}><Icon className="h-4 w-4" /></div>
        <div className="absolute bottom-[-17px] top-9 w-px bg-border" />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{previous ? `${previous.label} → ${next.label}` : next.label}</span>
          <Badge variant={next.tone}>{next.label}</Badge>
          {isLongest && <Badge variant="warning">Longest incident</Badge>}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-muted">{event.reason || 'No diagnostic reason was recorded.'}</p>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
          <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" /> {new Date(event.timestamp).toLocaleString()}</span>
          <span>{relativeTime(event.timestamp)}</span>
          <span className="inline-flex items-center gap-1"><Timer className="h-3.5 w-3.5" /> {formatDur(event.duration_sec)}</span>
        </div>
      </div>
      <ArrowRight className="hidden h-4 w-4 self-center text-muted sm:block" />
    </div>
  )
}

function ContextRow({ label, value }: { label: string; value: string }) {
  return <div className="flex items-start justify-between gap-3"><span className="text-muted">{label}</span><span className="text-right font-medium">{value}</span></div>
}
