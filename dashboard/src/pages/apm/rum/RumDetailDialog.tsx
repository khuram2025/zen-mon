import type { ReactNode } from 'react'
import {
  Activity,
  Clock3,
  FileWarning,
  Gauge,
  Layers3,
  Loader2,
  MousePointerClick,
  Network,
  Route,
  UserRound,
} from 'lucide-react'
import { formatBytes, relativeTime } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { fmtCount } from '@/components/apm/viz'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import type {
  RumAction,
  RumError,
  RumResource,
  RumSession,
  RumSessionDetail,
  RumTimelineEvent,
  RumView,
} from '@/types/apm'
import type { RumDetailKind } from './useRumUrlState'
import { QueryErrorPanel, RumCoverageNotice, RumMetricCell, TracePivot, formatDurationMs, formatRumVital } from './RumUi'

type Selected = RumView | RumSession | RumError | RumResource | RumAction | undefined

function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface2/35 p-3">
      <div className="text-[9px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-base font-semibold tabular-nums text-text">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid grid-cols-[110px,minmax(0,1fr)] gap-3 border-b border-border/60 py-2.5 text-xs last:border-0"><span className="text-muted">{label}</span><div className="min-w-0 break-words text-text2">{children || '—'}</div></div>
}

function ViewDetail({ view, onDrill }: { view: RumView; onDrill: (tab: 'sessions' | 'errors' | 'resources', viewName: string) => void }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Views" value={fmtCount(view.views)} />
        <Stat label="Sessions" value={fmtCount(view.sessions)} />
        <Stat label="Errors" value={fmtCount(view.errors)} hint={fmtPct(view.error_session_rate)} />
        <Stat label="Last seen" value={view.last_seen ? relativeTime(view.last_seen) : '—'} />
      </div>
      <Card><CardContent className="p-4">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted"><Gauge className="h-3.5 w-3.5 text-primary" /> Experience</div>
        <div className="grid grid-cols-3 gap-3">
          <RumMetricCell name="lcp" value={view.lcp_p75} samples={view.lcp_samples} />
          <RumMetricCell name="inp" value={view.inp_p75} samples={view.inp_samples} />
          <RumMetricCell name="cls" value={view.cls_p75} samples={view.cls_samples} />
          <RumMetricCell name="fcp" value={view.fcp_p75} samples={view.fcp_samples} />
          <RumMetricCell name="ttfb" value={view.ttfb_p75} samples={view.ttfb_samples} />
          <RumMetricCell name="load" value={view.load_p75} samples={view.load_samples} />
        </div>
      </CardContent></Card>
      <Card><CardContent className="p-4">
        <Fact label="Application">{view.application_id}</Fact><Fact label="Environment">{view.env}</Fact><Fact label="Route">{view.view_name}</Fact><Fact label="Example URL"><span className="font-mono text-[11px]">{view.url || 'Not captured'}</span></Fact><Fact label="Backend trace"><TracePivot traceId={view.backend_trace_id} /></Fact>
      </CardContent></Card>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => onDrill('sessions', view.view_name)}><UserRound className="h-3.5 w-3.5" /> Sessions on this view</Button>
        <Button variant="outline" size="sm" onClick={() => onDrill('errors', view.view_name)}><FileWarning className="h-3.5 w-3.5" /> Related errors</Button>
        <Button variant="outline" size="sm" onClick={() => onDrill('resources', view.view_name)}><Network className="h-3.5 w-3.5" /> Resources</Button>
      </div>
    </div>
  )
}

function ErrorDetail({ error }: { error: RumError }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3"><Stat label="Occurrences" value={fmtCount(error.count)} hint={(error.sampled_count != null || error.unsampled_count != null) ? `${fmtCount(error.sampled_count)} sampled · ${fmtCount(error.unsampled_count)} retained unsampled` : undefined} /><Stat label="Affected sessions" value={fmtCount(error.sessions)} /><Stat label="Last seen" value={relativeTime(error.last_seen)} /></div>
      <Card><CardContent className="p-4"><Fact label="Type">{error.error_type || 'JavaScript error'}</Fact><Fact label="Message"><span className="font-medium text-text">{error.message}</span></Fact><Fact label="Source"><span className="font-mono text-[11px]">{error.source || 'Not captured'}</span></Fact><Fact label="Fingerprint"><span className="font-mono text-[11px]">{error.fingerprint}</span></Fact><Fact label="Release">{error.service_version || 'Not captured'}</Fact><Fact label="View">{error.view_name || '/'}</Fact><Fact label="Client">{[error.browser && `${error.browser}${error.browser_version ? ` ${error.browser_version}` : ''}`, error.os, error.device_type, error.country].filter(Boolean).join(' · ') || 'Unknown'}</Fact><Fact label="Backend trace"><TracePivot traceId={error.backend_trace_id} /></Fact></CardContent></Card>
      {error.stack && <div><div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Latest stack trace</div><pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface2 p-3 font-mono text-[11px] leading-relaxed text-text2">{error.stack}</pre></div>}
    </div>
  )
}

function ResourceDetail({ resource }: { resource: RumResource }) {
  const failureRate = resource.failure_rate ?? (resource.count ? resource.failed_count / resource.count : null)
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4"><Stat label="Requests" value={fmtCount(resource.count)} /><Stat label="Failed" value={fmtCount(resource.failed_count)} hint={fmtPct(failureRate)} /><Stat label="Duration p75" value={formatDurationMs(resource.duration_p75)} /><Stat label="Average size" value={resource.size_avg == null ? '—' : formatBytes(resource.size_avg)} /></div>
      <Card><CardContent className="p-4"><Fact label="Name"><span className="font-mono text-[11px]">{resource.name}</span></Fact><Fact label="URL"><span className="font-mono text-[11px]">{resource.url || 'Not captured'}</span></Fact><Fact label="Kind">{resource.resource_type || 'resource'}</Fact><Fact label="HTTP">{[resource.method, resource.status_code].filter((value) => value != null).join(' ') || 'Not captured'}</Fact><Fact label="View">{resource.view_name || '/'}</Fact><Fact label="Application">{resource.application_id} · {resource.env}</Fact><Fact label="Release">{resource.service_version || 'Not captured'}</Fact><Fact label="Last seen">{relativeTime(resource.last_seen)}</Fact><Fact label="Backend trace"><TracePivot traceId={resource.backend_trace_id} /></Fact></CardContent></Card>
    </div>
  )
}

function ActionDetail({ action }: { action: RumAction }) {
  const frustration = ['rage_click', 'dead_click', 'error_click'].includes(action.action_type)
  return (
    <div className="space-y-4">
      {frustration && <div className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"><span className="font-semibold">Frustration signal:</span> this interaction pattern can indicate a broken or unresponsive experience.</div>}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3"><Stat label="Occurrences" value={fmtCount(action.count)} /><Stat label="Errors" value={fmtCount(action.error_count)} /><Stat label="Duration p75" value={formatDurationMs(action.duration_p75)} /></div>
      <Card><CardContent className="p-4"><Fact label="Action">{action.name || 'Unnamed action'}</Fact><Fact label="Type"><Badge variant={frustration ? 'warning' : 'outline'}>{action.action_type.replaceAll('_', ' ')}</Badge></Fact><Fact label="Target"><span className="font-mono text-[11px]">{action.target || 'Not captured'}</span></Fact><Fact label="View">{action.view_name || '/'}</Fact><Fact label="Application">{action.application_id} · {action.env}</Fact><Fact label="Release">{action.service_version || 'Not captured'}</Fact><Fact label="Last seen">{relativeTime(action.last_seen)}</Fact><Fact label="Backend trace"><TracePivot traceId={action.backend_trace_id} /></Fact></CardContent></Card>
    </div>
  )
}

const EVENT_META: Record<RumTimelineEvent['event_type'], { label: string; icon: typeof Activity; tone: string }> = {
  view: { label: 'View', icon: Route, tone: 'bg-primary/10 text-primary' },
  action: { label: 'Action', icon: MousePointerClick, tone: 'bg-info/10 text-info' },
  error: { label: 'Error', icon: FileWarning, tone: 'bg-danger/10 text-danger' },
  resource: { label: 'Resource', icon: Network, tone: 'bg-accent/10 text-accent' },
  long_task: { label: 'Long task', icon: Clock3, tone: 'bg-warning/10 text-warning' },
}

function attr(event: RumTimelineEvent, ...keys: string[]): string | undefined {
  for (const key of keys) if (event.attributes?.[key]) return event.attributes[key]
  return undefined
}

function timelineTitle(event: RumTimelineEvent): string {
  if (event.event_type === 'error') return event.error_message || attr(event, 'error.message') || 'JavaScript error'
  if (event.event_type === 'resource') return event.name || attr(event, 'resource.name', 'http.url') || event.url || 'Resource request'
  if (event.event_type === 'action') return event.name || attr(event, 'action.name') || event.action_type || 'User action'
  if (event.event_type === 'long_task') return `Main thread blocked for ${formatDurationMs(event.duration_ms)}`
  return event.view_name || event.url || 'Page view'
}

function Timeline({ events }: { events: RumTimelineEvent[] }) {
  if (!events.length) return <div className="py-10 text-center text-xs text-muted">No timeline events are available for this session.</div>
  return (
    <ol className="relative ml-3 border-l border-border pl-5">
      {events.map((event, index) => {
        const meta = EVENT_META[event.event_type] || EVENT_META.view
        const Icon = meta.icon
        const duration = event.duration_ms ?? (event.load_ms != null ? event.load_ms : undefined)
        return (
          <li key={`${event.timestamp}:${event.event_type}:${index}`} className="relative pb-5 last:pb-0">
            <span className={`absolute -left-[33px] flex h-6 w-6 items-center justify-center rounded-full border border-border ${meta.tone}`}><Icon className="h-3 w-3" /></span>
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0"><div className="max-w-md break-words text-xs font-medium text-text">{timelineTitle(event)}</div><div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] text-muted"><Badge variant="outline" className="px-1.5 py-0 text-[9px]">{meta.label}</Badge>{event.sampled === false && <Badge variant="warning" className="px-1.5 py-0 text-[9px]">retained unsampled</Badge>}<span>{new Date(event.timestamp).toLocaleString()}</span>{event.view_name && event.event_type !== 'view' && <span>· {event.view_name}</span>}</div></div>
              <div className="flex items-center gap-2">{duration != null && <span className="font-mono text-[10px] text-muted">{formatDurationMs(duration)}</span>}<TracePivot traceId={event.backend_trace_id} compact /></div>
            </div>
            {event.event_type === 'view' && (event.lcp != null || event.inp != null || event.cls != null) && <div className="mt-2 flex flex-wrap gap-2 rounded-md bg-surface2/50 p-2 font-mono text-[10px] text-text2"><span>LCP {formatRumVital('lcp', event.lcp)}</span><span>INP {formatRumVital('inp', event.inp)}</span><span>CLS {formatRumVital('cls', event.cls)}</span></div>}
            {event.stack && <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-surface2 p-2 font-mono text-[10px] text-text2">{event.stack}</pre>}
          </li>
        )
      })}
    </ol>
  )
}

function SessionDetailPanel({ fallback, detail, loading, error, onRetry }: { fallback?: RumSession; detail?: RumSessionDetail; loading?: boolean; error?: unknown; onRetry?: () => void }) {
  if (loading && !detail) return <div className="flex items-center justify-center gap-2 py-20 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading session timeline…</div>
  if (error) return <QueryErrorPanel label="session timeline" error={error} onRetry={onRetry} />
  const session = detail?.session ?? fallback
  if (!session) return <div className="py-12 text-center text-xs text-muted">This session is not available in the selected retention window.</div>
  const traceIds = [...new Set([...(session.backend_trace_ids ?? []), session.backend_trace_id || ''].filter(Boolean))]
  const loadedEvents = detail?.timeline.length ?? 0
  const totalEvents = detail?.total ?? loadedEvents
  return (
    <div className="space-y-5">
      {detail?.coverage && <div className="overflow-hidden rounded-lg border border-border"><RumCoverageNotice coverage={detail.coverage} showRetention /></div>}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4"><Stat label="Duration" value={formatDurationMs(session.duration_ms)} /><Stat label="Views" value={fmtCount(session.views)} /><Stat label="Actions" value={fmtCount(session.actions)} /><Stat label="Errors" value={fmtCount(session.errors)} /></div>
      <Card><CardContent className="p-4"><Fact label="Session ID"><span className="font-mono text-[11px]">{session.session_id}</span></Fact><Fact label="User">{session.user_id || 'Anonymous'}</Fact><Fact label="Application">{session.application_id} · {session.env}</Fact><Fact label="Release">{session.service_version || 'Not captured'}</Fact>{session.sampled != null && <Fact label="Sampling"><Badge variant={session.sampled ? 'success' : 'warning'}>{session.sampled ? 'Sampled cohort' : 'Retained unsampled'}</Badge></Fact>}<Fact label="Client">{[session.browser && `${session.browser}${session.browser_version ? ` ${session.browser_version}` : ''}`, session.os, session.device_type, session.country].filter(Boolean).join(' · ') || 'Unknown'}</Fact><Fact label="Started">{new Date(session.started_at).toLocaleString()}</Fact><Fact label="Backend traces">{traceIds.length ? <div className="flex flex-wrap gap-2">{traceIds.map((id) => <TracePivot key={id} traceId={id} compact />)}</div> : 'No correlated trace'}</Fact></CardContent></Card>
      <div><div className="mb-3 flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-text">Session timeline</h3><p className="text-[11px] text-muted">Views, actions, errors, resources and main-thread work in chronological order.{totalEvents > loadedEvents ? ` Showing the first ${loadedEvents.toLocaleString()} of ${totalEvents.toLocaleString()} events.` : ''}</p></div><Badge variant="outline">{loadedEvents.toLocaleString()}{totalEvents > loadedEvents ? ` / ${totalEvents.toLocaleString()}` : ''} events</Badge></div><Timeline events={detail?.timeline ?? []} /></div>
    </div>
  )
}

function titleFor(kind: RumDetailKind, selected: Selected, detail?: RumSessionDetail): { title: string; description: string; icon: typeof Activity } {
  if (kind === 'session') return { title: `Session ${(detail?.session ?? selected as RumSession | undefined)?.session_id?.slice(0, 14) ?? ''}`, description: 'Chronological real-user journey and backend correlation.', icon: UserRound }
  if (kind === 'error') return { title: (selected as RumError | undefined)?.error_type || 'JavaScript error', description: 'Impact, source context and representative backend trace.', icon: FileWarning }
  if (kind === 'resource') return { title: (selected as RumResource | undefined)?.name || 'Resource', description: 'Real-user request performance and failure context.', icon: Network }
  if (kind === 'action') return { title: (selected as RumAction | undefined)?.name || 'User action', description: 'Interaction volume, latency and frustration context.', icon: MousePointerClick }
  return { title: (selected as RumView | undefined)?.view_name || 'View details', description: 'Route-level performance and user impact.', icon: Layers3 }
}

export function RumDetailDialog({
  open,
  kind,
  selected,
  sessionDetail,
  sessionLoading,
  sessionError,
  onRetrySession,
  onClose,
  onDrill,
}: {
  open: boolean
  kind: RumDetailKind
  selected: Selected
  sessionDetail?: RumSessionDetail
  sessionLoading?: boolean
  sessionError?: unknown
  onRetrySession?: () => void
  onClose: () => void
  onDrill: (tab: 'sessions' | 'errors' | 'resources', viewName: string) => void
}) {
  const meta = titleFor(kind, selected, sessionDetail)
  const Icon = meta.icon
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose() }}>
      <DialogContent className="bottom-0 left-auto right-0 top-0 block h-screen w-[min(760px,calc(100vw-1rem))] max-w-none translate-x-0 translate-y-0 overflow-y-auto rounded-none border-y-0 border-r-0 p-0 sm:w-[min(760px,calc(100vw-3rem))]">
        <DialogHeader className="sticky top-0 z-10 border-b border-border bg-surface/95 px-5 py-4 pr-12 backdrop-blur">
          <DialogTitle className="flex items-center gap-2 text-base"><span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary"><Icon className="h-4 w-4" /></span><span className="min-w-0 truncate">{meta.title}</span></DialogTitle>
          <DialogDescription className="pl-10 text-xs">{meta.description}</DialogDescription>
        </DialogHeader>
        <div className="p-5">
          {kind === 'session' ? <SessionDetailPanel fallback={selected as RumSession | undefined} detail={sessionDetail} loading={sessionLoading} error={sessionError} onRetry={onRetrySession} />
            : kind === 'view' && selected ? <ViewDetail view={selected as RumView} onDrill={onDrill} />
              : kind === 'error' && selected ? <ErrorDetail error={selected as RumError} />
                : kind === 'resource' && selected ? <ResourceDetail resource={selected as RumResource} />
                  : kind === 'action' && selected ? <ActionDetail action={selected as RumAction} />
                    : <div className="py-16 text-center text-xs text-muted">This record is not on the current result page. Return to the list and open it again.</div>}
        </div>
      </DialogContent>
    </Dialog>
  )
}
