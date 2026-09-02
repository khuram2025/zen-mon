import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Activity,
  Clock3,
  Database,
  FileWarning,
  Gauge,
  Layers3,
  Loader2,
  Monitor,
  MousePointerClick,
  Network,
  Route,
  Server,
  UserRound,
  Waypoints,
  Wifi,
} from 'lucide-react'
import { cn, formatBytes, relativeTime } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { ApmTimeChart, fmtCount } from '@/components/apm/viz'
import { RequestFlow } from '@/components/apm/explorer'
import {
  BreakdownInline,
  LocationHint,
  PhaseBar,
  PhaseLegend,
  RequestPathFlow,
  phaseSegments,
} from './RumBreakdown'
import type { RumRequestTiming } from '@/types/apm'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import type {
  RumAction,
  RumError,
  RumErrorDetail,
  RumResource,
  RumSession,
  RumSessionDetail,
  RumTimelineEvent,
  RumView,
} from '@/types/apm'
import type { RumDetailKind } from './useRumUrlState'
import { ISSUE_STATUS_STYLE, IssueStatusBadge } from './RumTables'
import { QueryErrorPanel, RumCoverageNotice, RumVitalTile, TracePivot, formatDurationMs, formatRumVital } from './RumUi'

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
      <RequestFlow
        hops={[
          { id: 'client', label: 'Client', icon: UserRound, tone: 'ok' },
          { id: 'view', label: 'View', hint: view.view_name || '/', metric: formatRumVital('lcp', view.lcp_p75), icon: Layers3, tone: 'ok' },
          { id: 'app', label: 'App', hint: view.application_id, icon: Server, tone: view.errors > 0 ? 'warn' : 'ok' },
        ]}
      />
      <Card><CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted"><Gauge className="h-3.5 w-3.5 text-primary" /> Experience</div>
          <span className="text-[10px] text-muted">p75 · one sample per page view</span>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          <RumVitalTile name="lcp" value={view.lcp_p75} samples={view.lcp_samples} />
          <RumVitalTile name="inp" value={view.inp_p75} samples={view.inp_samples} />
          <RumVitalTile name="cls" value={view.cls_p75} samples={view.cls_samples} />
          <RumVitalTile name="fcp" value={view.fcp_p75} samples={view.fcp_samples} />
          <RumVitalTile name="ttfb" value={view.ttfb_p75} samples={view.ttfb_samples} />
          <RumVitalTile name="load" value={view.load_p75} samples={view.load_samples} />
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

const ISSUE_ACTIONS: Array<{ status: 'open' | 'resolved' | 'ignored'; label: string; hint: string }> = [
  { status: 'resolved', label: 'Resolve', hint: 'Fixed; reappearances will be flagged as regressed' },
  { status: 'ignored', label: 'Ignore', hint: 'Mute this group; it stays in the totals' },
  { status: 'open', label: 'Reopen', hint: 'Back to open' },
]

function CrumbIcon({ type }: { type: RumTimelineEvent['event_type'] }) {
  const meta = EVENT_META[type] || EVENT_META.view
  const Icon = meta.icon
  return <span className={cn('flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border', meta.tone)}><Icon className="h-2.5 w-2.5" /></span>
}

function StackFrames({ detail }: { detail: RumErrorDetail }) {
  const [showRaw, setShowRaw] = useState(false)
  const parsed = detail.frames.filter((frame) => frame.line != null)
  if (!detail.latest.stack) return <div className="rounded-lg border border-border bg-surface2/40 p-3 text-[11px] text-muted">No stack trace was captured with this error.</div>
  const symb = detail.symbolication
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Latest stack trace</div>
        <div className="flex items-center gap-2 text-[10px] text-muted">
          {symb.resolved > 0
            ? <span className="text-success">{symb.resolved} of {symb.frames} frames symbolicated{symb.release ? ` · release ${symb.release}` : ''}</span>
            : symb.maps_available > 0
              ? <span className="text-warning">{symb.maps_available} source map{symb.maps_available === 1 ? '' : 's'} uploaded for this release, none match these file names</span>
              : <span>Minified. Upload source maps under APM Settings → Source maps{symb.release ? ` for release ${symb.release}` : ''}.</span>}
          {parsed.length > 0 && <button type="button" className="underline hover:text-text" onClick={() => setShowRaw((value) => !value)}>{showRaw ? 'Show parsed' : 'Show raw'}</button>}
        </div>
      </div>
      {showRaw || !parsed.length ? (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface2 p-3 font-mono text-[11px] leading-relaxed text-text2">{detail.latest.stack}</pre>
      ) : (
        <ol className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border bg-surface2/40">
          {detail.frames.map((frame, index) => (
            <li key={index} className="px-3 py-2 font-mono text-[11px]">
              {frame.line == null ? (
                <div className="text-text2">{frame.raw.trim()}</div>
              ) : frame.symbolicated && frame.original ? (
                <>
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-semibold text-text">{frame.original.name || frame.function}</span>
                    <span className="text-primary">{frame.original.source}:{frame.original.line}:{frame.original.column}</span>
                    <span className="text-[10px] text-muted">from {frame.file_name}:{frame.line}:{frame.column}</span>
                  </div>
                  {frame.context.length > 0 && (
                    <div className="mt-1 overflow-x-auto rounded bg-surface px-2 py-1">
                      {frame.context.map((row) => (
                        <div key={row.line} className={cn('flex gap-3 whitespace-pre text-[10px] leading-relaxed', row.current ? 'bg-danger/10 text-text' : 'text-muted')}>
                          <span className="w-8 shrink-0 select-none text-right tabular-nums opacity-70">{row.line}</span>
                          <span>{row.code}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-text">{frame.function}</span>
                  <span className="text-muted">{frame.file_name || frame.url}:{frame.line}{frame.column != null ? `:${frame.column}` : ''}</span>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function ErrorDetail({ row: error, detail, loading, loadError, onRetry, onIssueStatus, issueUpdating, onOpenSession, onDrill }: {
  row?: RumError
  detail?: RumErrorDetail
  loading?: boolean
  loadError?: unknown
  onRetry?: () => void
  onIssueStatus?: (input: { fingerprint: string; application_id: string; env: string; status: 'open' | 'resolved' | 'ignored'; note: string; release: string }) => void
  issueUpdating?: boolean
  onOpenSession?: (sessionId: string) => void
  onDrill: (tab: 'sessions' | 'errors' | 'resources', viewName: string) => void
}) {
  const [note, setNote] = useState('')
  const issue = detail?.issue ?? error?.issue
  useEffect(() => { setNote(issue?.note ?? '') }, [issue?.note])
  const message = detail?.message ?? error?.message ?? ''
  const fingerprint = detail?.fingerprint ?? error?.fingerprint ?? ''
  const applicationId = detail?.application_id ?? error?.application_id ?? ''
  const env = detail?.env ?? error?.env ?? ''
  const viewName = detail?.latest.view_name ?? error?.view_name ?? ''
  const status = issue?.status ?? 'open'
  const style = ISSUE_STATUS_STYLE[status]
  if (loadError && !detail) return <QueryErrorPanel label="error group" error={loadError} onRetry={onRetry} />
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Occurrences" value={fmtCount(detail?.count ?? error?.count)} hint={detail && detail.unsampled_count > 0 ? `${fmtCount(detail.unsampled_count)} retained unsampled` : undefined} />
        <Stat label="Sessions" value={fmtCount(detail?.sessions ?? error?.sessions)} hint={detail ? `${fmtCount(detail.users)} identified user${detail.users === 1 ? '' : 's'}` : undefined} />
        <Stat label="First seen" value={detail?.first_seen_ever ? relativeTime(detail.first_seen_ever) : error?.first_seen ? relativeTime(error.first_seen) : '—'} hint={detail?.first_seen_release ? `release ${detail.first_seen_release}` : undefined} />
        <Stat label="Last seen" value={relativeTime(detail?.last_seen ?? error?.last_seen)} hint={detail?.latest.service_version ? `release ${detail.latest.service_version}` : undefined} />
      </div>

      <Card className={cn('border', status === 'new' && 'border-danger/40', status === 'regressed' && 'border-warning/40')}><CardContent className="p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <IssueStatusBadge issue={issue} />
            <span className="text-[11px] text-muted">{style.hint}{issue?.updated_by ? ` · ${issue.stored_status} by ${issue.updated_by}${issue.updated_at ? ` ${relativeTime(issue.updated_at)}` : ''}` : ''}</span>
          </div>
          <div className="flex items-center gap-1">
            {ISSUE_ACTIONS.filter((action) => action.status !== issue?.stored_status || (action.status === 'open' && status === 'regressed')).map((action) => (
              <Button key={action.status} size="sm" variant={action.status === 'resolved' ? 'default' : 'outline'} className="h-7 px-2 text-[11px]" disabled={!onIssueStatus || issueUpdating || !fingerprint} title={action.hint}
                onClick={() => onIssueStatus?.({ fingerprint, application_id: applicationId, env, status: action.status, note: note.trim(), release: detail?.latest.service_version ?? error?.service_version ?? '' })}>
                {issueUpdating ? <Loader2 className="h-3 w-3 animate-spin" /> : null}{action.label}
              </Button>
            ))}
          </div>
        </div>
        <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Note for the team (ticket, cause, fix version)…" maxLength={2000} className="mt-2 h-7 w-full rounded-md border border-border bg-surface px-2 text-[11px] text-text outline-none placeholder:text-muted focus:border-primary" />
      </CardContent></Card>

      <Card><CardContent className="p-4">
        <Fact label="Message"><span className="font-medium text-text">{message}</span></Fact>
        <Fact label="Type">{detail?.error_type ?? error?.error_type ?? 'JavaScript error'}</Fact>
        <Fact label="Source"><span className="font-mono text-[11px]">{detail?.source ?? error?.source ?? 'Not captured'}</span></Fact>
        <Fact label="Fingerprint"><span className="font-mono text-[11px]">{fingerprint}</span></Fact>
        <Fact label="Application">{applicationId}{env ? ` · ${env}` : ''}</Fact>
      </CardContent></Card>

      {loading && !detail ? <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading impact…</div> : detail && (
        <>
          <Card><CardContent className="p-3">
            <div className="mb-1 flex items-center justify-between gap-2 text-xs font-semibold uppercase tracking-wider text-muted"><span>Occurrences over time</span><span className="font-normal normal-case">{detail.trend.series.length} bucket{detail.trend.series.length === 1 ? '' : 's'}</span></div>
            <ApmTimeChart data={detail.trend.series} height={140} empty="No occurrences in this window." series={[
              { key: 'errors', name: 'Errors', color: '#ef4444', type: 'bar', fmt: fmtCount },
              { key: 'sessions', name: 'Sessions', color: '#f59e0b', fmt: fmtCount },
            ]} />
          </CardContent></Card>

          <div className="grid gap-2 sm:grid-cols-2">
            {([['view_name', 'Routes'], ['browser', 'Browsers'], ['service_version', 'Releases'], ['os', 'Operating systems']] as const).map(([key, title]) => (
              <Card key={key}><CardContent className="p-3">
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</div>
                {detail.facets[key].length ? (
                  <ul className="space-y-1">
                    {detail.facets[key].map((row) => (
                      <li key={row.value || '∅'} className="flex items-center justify-between gap-2 text-[11px]">
                        {key === 'view_name' ? <button type="button" className="truncate font-mono text-text2 hover:text-text hover:underline" onClick={() => onDrill('errors', row.value)} title={`Filter errors by ${row.value}`}>{row.value || '/'}</button> : <span className="truncate text-text2">{row.value || 'Unknown'}</span>}
                        <span className="shrink-0 font-mono tabular-nums text-muted">{fmtCount(row.count)} · {fmtCount(row.sessions)} sess.</span>
                      </li>
                    ))}
                  </ul>
                ) : <div className="text-[11px] text-muted">—</div>}
              </CardContent></Card>
            ))}
          </div>

          <StackFrames detail={detail} />

          <Card><CardContent className="p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">What the user did before the latest occurrence</div>
              {onOpenSession && detail.latest.session_id && <button type="button" className="text-[10px] text-primary hover:underline" onClick={() => onOpenSession(detail.latest.session_id)}>Open session {detail.latest.session_id.slice(0, 8)}…</button>}
            </div>
            {detail.breadcrumbs.length ? (
              <ol className="relative ml-2 space-y-1.5 border-l border-border/70 pl-4">
                {detail.breadcrumbs.map((crumb, index) => {
                  const isError = crumb.event_type === 'error' && index === detail.breadcrumbs.length - 1
                  return (
                    <li key={`${crumb.timestamp}:${index}`} className="relative text-[11px]">
                      <span className="absolute -left-[25px] top-0"><CrumbIcon type={crumb.event_type} /></span>
                      <div className="flex flex-wrap items-baseline gap-x-2">
                        <span className={cn('truncate', isError ? 'font-medium text-danger' : 'text-text')}>{crumb.title}</span>
                        {crumb.status_code != null && crumb.status_code >= 400 && <span className="font-mono text-[10px] text-danger">{crumb.status_code}</span>}
                        {crumb.duration_ms != null && crumb.duration_ms > 0 && <span className="font-mono text-[10px] text-muted">{formatDurationMs(crumb.duration_ms)}</span>}
                        <span className="text-[10px] text-muted">{crumb.view_name ? `on ${crumb.view_name} · ` : ''}{new Date(crumb.timestamp).toLocaleTimeString()}</span>
                      </div>
                    </li>
                  )
                })}
              </ol>
            ) : <div className="text-[11px] text-muted">No earlier activity was recorded in that session.</div>}
          </CardContent></Card>

          <Card><CardContent className="p-4">
            <Fact label="Latest client">{[detail.latest.browser && `${detail.latest.browser}${detail.latest.browser_version ? ` ${detail.latest.browser_version}` : ''}`, detail.latest.os, detail.latest.device_type, detail.latest.country].filter(Boolean).join(' · ') || 'Unknown'}</Fact>
            <Fact label="Page"><span className="font-mono text-[11px]">{detail.latest.url || viewName || '/'}</span></Fact>
            <Fact label="User">{detail.latest.user_id || 'Anonymous'}{detail.latest.client_ip ? <span className="ml-2 font-mono text-[10px] text-muted">{detail.latest.client_ip}</span> : null}</Fact>
            <Fact label="Backend trace"><TracePivot traceId={detail.latest.backend_trace_id || detail.backend_trace_ids[0]} /></Fact>
            <Fact label="Recent sessions">{detail.recent_sessions.length ? (
              <div className="flex flex-wrap gap-1.5">
                {detail.recent_sessions.map((session) => (
                  <button key={session.session_id} type="button" className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-[10px] text-text2 hover:border-primary/40 hover:text-text" title={`${session.count} occurrence${session.count === 1 ? '' : 's'} · ${session.browser} · ${relativeTime(session.last_seen)}`} onClick={() => onOpenSession?.(session.session_id)}>{session.session_id.slice(0, 10)}…</button>
                ))}
              </div>
            ) : '—'}</Fact>
          </CardContent></Card>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => onDrill('sessions', viewName)}><UserRound className="h-3.5 w-3.5" /> Sessions on this view</Button>
      </div>
    </div>
  )
}

/**
 * Everything the resource explorer knows about one request group, folded
 * into the same shape the session timeline uses. Three data sources feed it,
 * each of which can be missing independently:
 *  - Resource Timing phases (DNS, connect, wait, download): SDK 2.1+ and,
 *    for cross-origin hosts, a Timing-Allow-Origin header from that host.
 *  - Server-Timing header from the backend: app / db execution split.
 *  - Correlated APM traces: the same split, derived from the SERVER and
 *    database spans of the traces this request carried.
 */
export function resourceTimingModel(resource: RumResource) {
  const timingSamples = resource.timing_samples ?? 0
  const opaque = timingSamples > 0 && (resource.opaque_samples ?? 0) >= timingSamples
  const hasPhases = timingSamples > 0 && !opaque
  const hasServerTiming = (resource.server_samples ?? 0) > 0
  const backend = resource.backend ?? null
  const timing: RumRequestTiming | null = hasPhases ? {
    redirect_ms: 0,
    dns_ms: resource.dns_p75 ?? 0,
    connect_ms: resource.connect_p75 ?? 0,
    tls_ms: resource.tls_p75 ?? 0,
    wait_ms: resource.wait_p75 ?? 0,
    download_ms: resource.download_p75 ?? 0,
    blocked_ms: 0,
    processing_ms: 0,
    server_ms: resource.server_p75 ?? 0,
    db_ms: resource.db_p75 ?? 0,
    has_server_timing: hasServerTiming,
    protocol: resource.protocol,
  } : null
  let segments = timing ? phaseSegments(timing, backend) : []
  // No browser phases, but a backend split and a total: derive the network
  // share as total − server so the bar still tells where the time went.
  if (!segments.length && backend && backend.server_ms > 0 && (resource.duration_p75 ?? 0) > 0) {
    const total = resource.duration_p75 ?? 0
    const server = Math.min(backend.server_ms, total)
    const db = Math.min(backend.db_ms, server)
    segments = phaseSegments({
      redirect_ms: 0, dns_ms: 0, connect_ms: 0, tls_ms: 0, blocked_ms: 0, processing_ms: 0, download_ms: 0,
      wait_ms: total, server_ms: server, db_ms: db, has_server_timing: true,
    })
  }
  const isStaticAsset = !['fetch', 'xhr'].includes(String(resource.resource_type || '').toLowerCase())
  const oldSdk = (resource.sdk_versions ?? []).some((version) => !version || Number(version.split('.')[0]) < 2 || (version.startsWith('2.0')))
  const reasons: string[] = []
  if (timingSamples === 0) {
    reasons.push(oldSdk
      ? `Recorded by browser SDK ${resource.sdk_versions?.filter(Boolean).join(', ') || '2.0'} — phase timing (DNS, connect, wait, download) needs SDK 2.1 or newer. New page loads pick up the current SDK automatically.`
      : 'No Resource Timing entry was captured for these requests.')
  } else if (opaque) {
    reasons.push('The browser reported this cross-origin request as opaque: only the total duration is exposed until that host responds with a Timing-Allow-Origin header.')
  }
  if (!hasServerTiming && !backend && !isStaticAsset) {
    reasons.push('No application / database split: the backend sent no Server-Timing header and none of these requests carried a trace into an APM-instrumented service.')
  }
  if (!hasPhases && backend && backend.server_ms > 0 && (resource.duration_p75 ?? 0) > 0 && backend.server_ms >= (resource.duration_p75 ?? 0)) {
    reasons.push(`The backend average (${formatDurationMs(backend.server_ms)} over ${backend.traces} trace${backend.traces === 1 ? '' : 's'}) is at or above the browser p75 total, so no network share can be separated for this group.`)
  }
  return { timing, segments, backend, hasPhases, hasServerTiming, opaque, isStaticAsset, reasons }
}

function ResourceDetail({ resource }: { resource: RumResource }) {
  const failureRate = resource.failure_rate ?? (resource.count ? resource.failed_count / resource.count : null)
  const model = resourceTimingModel(resource)
  const { timing, segments, backend } = model
  const showFlow = !!timing || !!backend
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4"><Stat label="Requests" value={fmtCount(resource.count)} /><Stat label="Failed" value={fmtCount(resource.failed_count)} hint={fmtPct(failureRate)} /><Stat label="Duration p75" value={formatDurationMs(resource.duration_p75)} /><Stat label="Average size" value={resource.size_avg == null ? '—' : formatBytes(resource.size_avg)} /></div>
      {showFlow ? (
        <RequestPathFlow
          timing={timing ?? (backend && (resource.duration_p75 ?? 0) > 0 ? {
            redirect_ms: 0, dns_ms: 0, connect_ms: 0, tls_ms: 0, blocked_ms: 0, processing_ms: 0, download_ms: 0,
            wait_ms: resource.duration_p75 ?? 0, server_ms: 0, db_ms: 0, has_server_timing: false, protocol: resource.protocol,
          } : null)}
          backend={backend}
          clientHint={resource.view_name || '/'}
          totalMs={resource.duration_p75}
          status={resource.status_code}
        />
      ) : (
        <RequestFlow
          hops={[
            { id: 'client', label: 'Client', icon: UserRound, tone: 'ok' },
            { id: 'resource', label: String(resource.method || resource.resource_type || 'GET'), hint: resource.name, metric: formatDurationMs(resource.duration_p75), status: resource.status_code, icon: Network, tone: resource.failed_count ? 'err' : 'ok' },
            { id: 'app', label: model.isStaticAsset ? 'Static asset' : 'App', hint: model.isStaticAsset ? 'no server execution' : resource.view_name || '/', icon: Server, tone: 'muted' },
          ]}
          totalLabel={formatDurationMs(resource.duration_p75)}
        />
      )}
      <Card><CardContent className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted"><Waypoints className="h-3.5 w-3.5 text-primary" /> Where the time goes</div>
          <span className="text-[10px] text-muted">
            {model.hasPhases ? `p75 across ${fmtCount(resource.timing_samples)} measured requests` : `${fmtCount(resource.count)} requests`}
            {model.hasServerTiming ? ` · ${fmtCount(resource.server_samples)} with Server-Timing` : backend ? ` · ${fmtCount(backend.traces)} correlated trace${backend.traces === 1 ? '' : 's'}` : ''}
          </span>
        </div>
        {segments.length > 0 ? (
          <>
            <PhaseBar segments={segments} height={10} />
            <PhaseLegend segments={segments} className="mt-2" />
            {!model.hasPhases && backend && <p className="mt-2 text-[10px] text-muted">Browser phases are unavailable for these requests, so the network share is derived as total time minus the backend&apos;s execution time from {backend.service ? <span className="font-mono">{backend.service}</span> : 'the correlated traces'}.</p>}
          </>
        ) : (
          <p className="text-[11px] text-muted">Only the total duration is known for these requests.</p>
        )}
        {model.reasons.length > 0 && (
          <ul className="mt-2 space-y-1 border-t border-border/60 pt-2 text-[10px] leading-snug text-muted">
            {model.reasons.map((reason) => <li key={reason}>• {reason}</li>)}
          </ul>
        )}
      </CardContent></Card>
      <Card><CardContent className="p-4"><Fact label="Name"><span className="font-mono text-[11px]">{resource.name}</span></Fact><Fact label="URL"><span className="font-mono text-[11px]">{resource.url || 'Not captured'}</span></Fact><Fact label="Kind">{resource.resource_type || 'resource'}{resource.protocol ? ` · ${resource.protocol}` : ''}</Fact><Fact label="HTTP">{[resource.method, resource.status_code].filter((value) => value != null).join(' ') || 'Not captured'}</Fact><Fact label="View">{resource.view_name || '/'}</Fact><Fact label="Application">{resource.application_id} · {resource.env}</Fact><Fact label="Release">{resource.service_version || 'Not captured'}</Fact><Fact label="Last seen">{relativeTime(resource.last_seen)}</Fact><Fact label="Backend trace"><TracePivot traceId={resource.backend_trace_id} /></Fact></CardContent></Card>
    </div>
  )
}

function ActionDetail({ action }: { action: RumAction }) {
  const frustration = ['rage_click', 'dead_click', 'error_click'].includes(action.action_type)
  return (
    <div className="space-y-4">
      {frustration && <div className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"><span className="font-semibold">Frustration signal:</span> this interaction pattern can indicate a broken or unresponsive experience.</div>}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3"><Stat label="Occurrences" value={fmtCount(action.count)} /><Stat label="Errors" value={fmtCount(action.error_count)} /><Stat label="Duration p75" value={formatDurationMs(action.duration_p75)} /></div>
      <RequestFlow
        hops={[
          { id: 'client', label: 'Client', icon: UserRound, tone: 'ok' },
          { id: 'action', label: (action.action_type || 'action').replaceAll('_', ' '), hint: action.name || action.target, metric: formatDurationMs(action.duration_p75), icon: MousePointerClick, tone: frustration ? 'warn' : 'ok' },
          { id: 'app', label: 'App', hint: action.view_name || '/', icon: Server, tone: action.error_count ? 'err' : 'muted' },
        ]}
        totalLabel={formatDurationMs(action.duration_p75)}
      />
      <Card><CardContent className="p-4"><Fact label="Action">{action.name || 'Unnamed action'}</Fact><Fact label="Type"><Badge variant={frustration ? 'warning' : 'outline'}>{(action.action_type || 'action').replaceAll('_', ' ')}</Badge></Fact><Fact label="Target"><span className="font-mono text-[11px]">{action.target || 'Not captured'}</span></Fact><Fact label="View">{action.view_name || '/'}</Fact><Fact label="Application">{action.application_id} · {action.env}</Fact><Fact label="Release">{action.service_version || 'Not captured'}</Fact><Fact label="Last seen">{relativeTime(action.last_seen)}</Fact><Fact label="Backend trace"><TracePivot traceId={action.backend_trace_id} /></Fact></CardContent></Card>
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

const CHILD_TYPES = ['action', 'resource', 'error', 'long_task'] as const
type ChildType = (typeof CHILD_TYPES)[number]
const CHILD_LABEL: Record<ChildType, string> = { action: 'action', resource: 'resource', error: 'error', long_task: 'long task' }

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

function offsetLabel(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '+0s'
  if (ms < 1000) return `+${Math.round(ms)}ms`
  if (ms < 60_000) return `+${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `+${minutes}m${seconds ? ` ${seconds}s` : ''}`
}

interface ViewSegment {
  key: string
  viewId?: string
  viewName: string
  startMs: number
  endMs: number
  vitals: RumTimelineEvent | null
  backendTraceId?: string
  children: RumTimelineEvent[]
}

/**
 * Collapse the raw event stream into one node per page view. A session emits
 * many `view` lifecycle events (view_start, checkpoint, pagehide, hidden,
 * final) for the same navigation; rendering each as its own row buries the
 * user journey. Here every navigation becomes a single grouped segment that
 * carries its finalized Web Vitals, and the actions / resources / errors that
 * happened on that view are nested beneath it in chronological order.
 */
function buildSegments(events: RumTimelineEvent[]): ViewSegment[] {
  const segments: ViewSegment[] = []
  // Lifecycle events of one navigation may arrive out of order when they
  // share a millisecond (a finalized record sorted ahead of its view_start);
  // keying by view_id keeps every event of a view in its own segment.
  const byViewId = new Map<string, ViewSegment>()
  const open = (event: RumTimelineEvent, ts: number, index: number, isView: boolean): ViewSegment => {
    const seg: ViewSegment = {
      key: `${event.view_id || event.view_name || (isView ? 'view' : 'activity')}:${ts}:${index}`,
      viewId: event.view_id,
      viewName: event.view_name || event.url || (isView ? '/' : 'Session activity'),
      startMs: ts,
      endMs: ts,
      vitals: null,
      backendTraceId: isView ? (event.backend_trace_id || undefined) : undefined,
      children: [],
    }
    segments.push(seg)
    if (isView && event.view_id) byViewId.set(event.view_id, seg)
    return seg
  }
  events.forEach((event, index) => {
    const ts = new Date(event.timestamp).getTime()
    const last = segments[segments.length - 1]
    if (event.event_type === 'view') {
      const known = event.view_id ? byViewId.get(event.view_id) : undefined
      const startsNew = !known && (!last || event.end_reason === 'view_start' || (!!event.view_id && event.view_id !== last.viewId))
      const seg = known ?? (startsNew ? open(event, ts, index, true) : last!)
      seg.startMs = Math.min(seg.startMs, ts)
      seg.endMs = Math.max(seg.endMs, ts)
      if (!seg.backendTraceId && event.backend_trace_id) seg.backendTraceId = event.backend_trace_id
      const hasVitals = event.lcp != null || event.inp != null || event.cls != null || event.fcp != null || event.ttfb != null || event.load_ms != null
      if (hasVitals && (!seg.vitals || event.is_final)) seg.vitals = event
    } else {
      const seg = last ?? open(event, ts, index, false)
      seg.endMs = Math.max(seg.endMs, ts)
      seg.children.push(event)
    }
  })
  return segments
}

function childSummary(children: RumTimelineEvent[]): string {
  const counts = children.reduce<Record<string, number>>((acc, event) => {
    acc[event.event_type] = (acc[event.event_type] ?? 0) + 1
    return acc
  }, {})
  return CHILD_TYPES
    .filter((type) => counts[type])
    .map((type) => `${counts[type]} ${CHILD_LABEL[type]}${counts[type] > 1 ? 's' : ''}`)
    .join(' · ')
}

function TimelineChild({ event, sessionStart }: { event: RumTimelineEvent; sessionStart: number }) {
  const meta = EVENT_META[event.event_type] || EVENT_META.view
  const Icon = meta.icon
  const duration = event.duration_ms ?? undefined
  return (
    <li className="relative py-1.5">
      <span className={cn('absolute -left-[25px] flex h-4 w-4 items-center justify-center rounded-full border border-border', meta.tone)}><Icon className="h-2.5 w-2.5" /></span>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            {event.event_type === 'resource' && event.method && <Badge variant="outline" className="px-1 py-0 text-[9px] uppercase">{event.method}</Badge>}
            {event.event_type === 'resource' && event.status_code != null && <span className={cn('font-mono text-[9px]', event.status_code >= 400 ? 'text-danger' : 'text-muted')}>{event.status_code}</span>}
            <span className={cn('max-w-md break-words text-xs', event.event_type === 'error' ? 'font-medium text-danger' : 'text-text')}>{timelineTitle(event)}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] text-muted">
            <Badge variant="outline" className="px-1.5 py-0 text-[9px]">{meta.label}</Badge>
            {event.sampled === false && <Badge variant="warning" className="px-1.5 py-0 text-[9px]">retained unsampled</Badge>}
            <span className="font-mono">{offsetLabel(new Date(event.timestamp).getTime() - sessionStart)}</span>
            <span>· {new Date(event.timestamp).toLocaleTimeString()}</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {duration != null && duration > 0 && <span className="font-mono text-[10px] text-muted">{formatDurationMs(duration)}</span>}
          <TracePivot traceId={event.backend_trace_id} compact />
        </div>
      </div>
      {event.timing && (event.event_type === 'resource' || event.event_type === 'view') && (() => {
        const segments = phaseSegments(event.timing, event.backend)
        if (!segments.length) return null
        return (
          <div className="mt-1.5 pr-1">
            <PhaseBar segments={segments} height={5} />
            <BreakdownInline timing={event.timing} backend={event.backend} className="mt-1" />
          </div>
        )
      })()}
      {event.stack && <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-surface2 p-2 font-mono text-[10px] text-text2">{event.stack}</pre>}
    </li>
  )
}

function SegmentRow({ seg, sessionStart, hidden }: { seg: ViewSegment; sessionStart: number; hidden: Set<ChildType> }) {
  const [mode, setMode] = useState<'list' | 'waterfall'>('list')
  const duration = seg.endMs - seg.startMs
  const vitals = seg.vitals
  const summary = childSummary(seg.children)
  const visibleChildren = seg.children.filter((event) => !hidden.has(event.event_type as ChildType))
  return (
    <li className="overflow-hidden rounded-lg border border-border bg-surface2/30">
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border/60 bg-surface2/50 px-3 py-2">
        <div className="flex min-w-0 items-start gap-2">
          <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary"><Route className="h-3 w-3" /></span>
          <div className="min-w-0">
            <span className="block truncate font-mono text-xs font-semibold text-text" title={seg.viewName}>{seg.viewName || '/'}</span>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-muted">
              <Badge variant="outline" className="px-1.5 py-0 text-[9px]">Page view</Badge>
              <span className="font-mono">{offsetLabel(seg.startMs - sessionStart)}</span>
              {duration > 0 && <span>· {formatDurationMs(duration)} on view</span>}
              {summary && <span>· {summary}</span>}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {vitals && (vitals.lcp != null || vitals.inp != null || vitals.cls != null) && (
            <div className="hidden gap-1.5 font-mono text-[10px] text-text2 sm:flex">
              {vitals.lcp != null && <span>LCP {formatRumVital('lcp', vitals.lcp)}</span>}
              {vitals.inp != null && <span>INP {formatRumVital('inp', vitals.inp)}</span>}
              {vitals.cls != null && <span>CLS {formatRumVital('cls', vitals.cls)}</span>}
            </div>
          )}
          {seg.backendTraceId && <TracePivot traceId={seg.backendTraceId} compact />}
        </div>
      </div>
      {vitals?.vital_attribution && Object.keys(vitals.vital_attribution).length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-border/60 bg-surface2/20 px-3 py-1.5 text-[10px] text-muted">
          <span className="font-semibold uppercase tracking-wider">Attribution</span>
          {vitals.vital_attribution['lcp.element'] && <span title={vitals.vital_attribution['lcp.url'] ? `LCP resource: ${vitals.vital_attribution['lcp.url']}` : 'Largest Contentful Paint element'}>LCP <span className="font-mono text-text2">{vitals.vital_attribution['lcp.element']}</span></span>}
          {vitals.vital_attribution['cls.element'] && <span title="Element that shifted most">CLS <span className="font-mono text-text2">{vitals.vital_attribution['cls.element']}</span></span>}
          {vitals.vital_attribution['inp.target'] && <span title="Slowest interaction target">INP <span className="font-mono text-text2">{vitals.vital_attribution['inp.target']}</span>{vitals.vital_attribution['inp.event_type'] ? ` (${vitals.vital_attribution['inp.event_type']})` : ''}</span>}
        </div>
      )}
      {vitals?.timing && (() => {
        const segments = phaseSegments(vitals.timing, vitals.backend)
        if (!segments.length) return null
        return (
          <div className="border-b border-border/60 bg-surface2/20 px-3 py-2">
            <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-muted">Page load breakdown{vitals.timing.protocol ? ` · ${vitals.timing.protocol}` : ''}</div>
            <PhaseBar segments={segments} height={6} />
            <PhaseLegend segments={segments} className="mt-1.5" />
          </div>
        )
      })()}
      {visibleChildren.length > 0 && (
        <>
          <div className="flex items-center justify-end gap-1 border-b border-border/40 px-3 py-1 text-[10px] text-muted">
            <button type="button" className={cn('rounded px-1.5 py-0.5', mode === 'list' ? 'bg-primary/10 text-primary' : 'hover:text-text')} onClick={() => setMode('list')}>List</button>
            <button type="button" className={cn('rounded px-1.5 py-0.5', mode === 'waterfall' ? 'bg-primary/10 text-primary' : 'hover:text-text')} onClick={() => setMode('waterfall')} title="Requests and interactions laid out on the view's timeline">Waterfall</button>
          </div>
          {mode === 'waterfall' ? (
            <Waterfall events={visibleChildren} startMs={seg.startMs} />
          ) : (
            <ol className="relative ml-5 border-l border-border/70 py-1 pl-4 pr-3">
              {visibleChildren.map((event, index) => (
                <TimelineChild key={`${event.timestamp}:${event.event_type}:${index}`} event={event} sessionStart={sessionStart} />
              ))}
            </ol>
          )}
        </>
      )}
    </li>
  )
}

/**
 * Gantt-style layout of one view's requests and interactions: each row starts
 * at its offset from the view start and spans its duration, split into the
 * request phases when the SDK captured them. Resource events are recorded at
 * completion, so their bar is placed to end at the event timestamp.
 */
function Waterfall({ events, startMs }: { events: RumTimelineEvent[]; startMs: number }) {
  const rows = events.map((event) => {
    const ts = new Date(event.timestamp).getTime()
    const duration = Math.max(0, event.duration_ms ?? 0)
    const endsAt = event.event_type === 'resource' ? ts : ts + duration
    const beginsAt = event.event_type === 'resource' ? ts - duration : ts
    return { event, begin: Math.max(0, beginsAt - startMs), end: Math.max(0, endsAt - startMs), duration }
  })
  const span = Math.max(1, ...rows.map((row) => row.end))
  const tickCount = 5
  return (
    <div className="px-3 py-2">
      <div className="relative ml-[200px] mb-1 h-3 text-[9px] text-muted">
        {Array.from({ length: tickCount + 1 }, (_, index) => {
          const value = (span / tickCount) * index
          return <span key={index} className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: `${(index / tickCount) * 100}%` }}>{offsetLabel(value)}</span>
        })}
      </div>
      <ol className="space-y-0.5">
        {rows.map(({ event, begin, end, duration }, index) => {
          const meta = EVENT_META[event.event_type] || EVENT_META.view
          const segments = event.timing && event.event_type === 'resource' ? phaseSegments(event.timing, event.backend) : []
          const left = (begin / span) * 100
          const width = Math.max(0.6, ((end - begin) / span) * 100)
          const failed = event.event_type === 'error' || (event.status_code != null && event.status_code >= 400)
          return (
            <li key={`${event.timestamp}:${index}`} className="flex items-center gap-2 text-[10px]">
              <div className="flex w-[192px] shrink-0 items-center gap-1.5 overflow-hidden">
                <span className={cn('flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border border-border', meta.tone)}><meta.icon className="h-2 w-2" /></span>
                <span className={cn('truncate', failed ? 'text-danger' : 'text-text2')} title={timelineTitle(event)}>{timelineTitle(event)}</span>
              </div>
              <div className="relative h-3.5 flex-1 rounded bg-surface2/60" title={`${timelineTitle(event)} · starts ${offsetLabel(begin)} · ${duration > 0 ? formatDurationMs(duration) : 'instant'}`}>
                {duration > 0 ? (
                  <div className="absolute top-0 h-full overflow-hidden rounded" style={{ left: `${left}%`, width: `${width}%` }}>
                    {segments.length ? <PhaseBar segments={segments} height={14} className="rounded" /> : <div className={cn('h-full w-full rounded', failed ? 'bg-danger/70' : event.event_type === 'action' ? 'bg-info/70' : event.event_type === 'long_task' ? 'bg-warning/70' : 'bg-accent/60')} />}
                  </div>
                ) : (
                  <div className={cn('absolute top-0.5 h-2.5 w-2.5 rotate-45 rounded-sm', failed ? 'bg-danger' : 'bg-info')} style={{ left: `calc(${left}% - 5px)` }} />
                )}
              </div>
              <span className="w-14 shrink-0 text-right font-mono tabular-nums text-muted">{duration > 0 ? formatDurationMs(duration) : ''}</span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function Timeline({ events }: { events: RumTimelineEvent[] }) {
  const segments = useMemo(() => buildSegments(events), [events])
  const counts = useMemo(() => events.reduce<Record<ChildType, number>>((acc, event) => {
    if ((CHILD_TYPES as readonly string[]).includes(event.event_type)) acc[event.event_type as ChildType] += 1
    return acc
  }, { action: 0, resource: 0, error: 0, long_task: 0 }), [events])
  const [hidden, setHidden] = useState<Set<ChildType>>(new Set())
  if (!events.length) return <div className="py-10 text-center text-xs text-muted">No timeline events are available for this session.</div>
  const sessionStart = new Date(events[0].timestamp).getTime()
  const toggle = (type: ChildType) => setHidden((prev) => {
    const next = new Set(prev)
    if (next.has(type)) next.delete(type)
    else next.add(type)
    return next
  })
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Show</span>
        {CHILD_TYPES.map((type) => {
          const meta = EVENT_META[type]
          const Icon = meta.icon
          const off = hidden.has(type)
          const empty = !counts[type]
          return (
            <button
              key={type}
              type="button"
              onClick={() => toggle(type)}
              disabled={empty}
              aria-pressed={!off}
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition',
                off ? 'border-border bg-transparent text-muted' : 'border-primary/25 bg-primary/10 text-text2',
                empty && 'cursor-not-allowed opacity-40',
              )}
            >
              <Icon className="h-3 w-3" /> {CHILD_LABEL[type].replace(/^\w/, (character) => character.toUpperCase())}s
              <span className="tabular-nums">{counts[type]}</span>
            </button>
          )
        })}
      </div>
      <ol className="space-y-2.5">
        {segments.map((seg) => <SegmentRow key={seg.key} seg={seg} sessionStart={sessionStart} hidden={hidden} />)}
      </ol>
    </div>
  )
}

/**
 * Session-level Client → Browser → Network → App → Database chain. Per-hop
 * figures are averaged over this session's measured requests; the app/db side
 * comes from Server-Timing captures or correlated backend traces.
 */
function SessionPathFlow({ session, detail, traceIds }: { session: RumSession; detail?: RumSessionDetail; traceIds: string[] }) {
  const stats = useMemo(() => {
    let netSum = 0, netN = 0, serverSum = 0, serverN = 0, dbSum = 0, dbN = 0, measured = 0
    for (const ev of detail?.timeline ?? []) {
      if (ev.event_type !== 'resource') continue
      const t = ev.timing
      const serverMs = t?.has_server_timing && t.server_ms > 0 ? t.server_ms : (ev.backend && ev.backend.server_ms > 0 ? ev.backend.server_ms : null)
      const dbMs = t?.has_server_timing && t.db_ms > 0 ? t.db_ms : (ev.backend && ev.backend.db_ms > 0 ? ev.backend.db_ms : null)
      if (t && t.wait_ms > 0) measured += 1
      if (serverMs != null) { serverSum += serverMs; serverN += 1 }
      if (dbMs != null) { dbSum += dbMs; dbN += 1 }
      if (t && t.wait_ms > 0 && serverMs != null) { netSum += Math.max(0, t.wait_ms - Math.min(serverMs, t.wait_ms)); netN += 1 }
    }
    const summary = detail?.backend_summary
    return {
      measured,
      net: netN ? netSum / netN : null,
      server: serverN ? serverSum / serverN : summary?.avg_server_ms ?? null,
      db: dbN ? dbSum / dbN : summary?.avg_db_ms ?? null,
      service: summary?.services?.[0] ?? '',
      dbSystems: summary?.db_systems ?? [],
    }
  }, [detail])
  const hops: Parameters<typeof RequestFlow>[0]['hops'] = [
    {
      id: 'client',
      label: 'Client',
      hint: [session.browser && `${session.browser}${session.browser_version ? ` ${session.browser_version}` : ''}`, session.os, session.client_ip].filter(Boolean).join(' · ') || 'Unknown',
      metric: session.connection_rtt_ms != null && session.connection_rtt_ms > 0 ? `~${Math.round(session.connection_rtt_ms)} ms RTT` : undefined,
      icon: UserRound,
      tone: 'ok',
    },
    { id: 'browser', label: 'Browser', hint: `${session.views} views · ${session.actions} actions`, metric: formatDurationMs(session.duration_ms), icon: Monitor, tone: session.errors ? 'warn' : 'ok' },
    {
      id: 'network',
      label: 'Network',
      hint: stats.net != null ? `avg over ${stats.measured} requests` : (stats.measured ? 'no server split yet' : 'no measured requests'),
      metric: stats.net != null ? formatDurationMs(stats.net) : undefined,
      icon: Waypoints,
      tone: 'muted',
    },
    {
      id: 'app',
      label: stats.service || 'Backend',
      hint: traceIds.length ? `${traceIds.length} correlated trace${traceIds.length > 1 ? 's' : ''}` : 'No correlated trace',
      metric: stats.server != null ? formatDurationMs(Math.max(0, stats.server - (stats.db ?? 0))) : undefined,
      icon: Server,
      tone: traceIds.length || stats.server != null ? 'ok' : 'muted',
    },
  ]
  if (stats.db != null && stats.db > 0) {
    hops.push({
      id: 'db',
      label: 'Database',
      hint: stats.dbSystems.length ? stats.dbSystems.join(', ') : 'reported by server',
      metric: formatDurationMs(stats.db),
      icon: Database,
      tone: 'ok',
    })
  }
  return <RequestFlow hops={hops} totalLabel={formatDurationMs(session.duration_ms)} />
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
      <SessionPathFlow session={session} detail={detail} traceIds={traceIds} />
      <Card><CardContent className="p-4"><Fact label="Session ID"><span className="font-mono text-[11px]">{session.session_id}</span></Fact><Fact label="User">{session.user_id || 'Anonymous'}</Fact><Fact label="Application">{session.application_id} · {session.env}</Fact><Fact label="Release">{session.service_version || 'Not captured'}</Fact>{session.sampled != null && <Fact label="Sampling"><Badge variant={session.sampled ? 'success' : 'warning'}>{session.sampled ? 'Sampled cohort' : 'Retained unsampled'}</Badge></Fact>}<Fact label="Client">{[session.browser && `${session.browser}${session.browser_version ? ` ${session.browser_version}` : ''}`, session.os, session.device_type].filter(Boolean).join(' · ') || 'Unknown'}</Fact><Fact label="Location"><LocationHint country={session.country} timezone={session.timezone} clientIp={session.client_ip} /></Fact><Fact label="IP address"><span className="font-mono text-[11px]">{session.client_ip || 'Not captured'}</span></Fact><Fact label="Connection">{session.connection_type || session.connection_rtt_ms != null ? <span className="inline-flex items-center gap-1.5"><Wifi className="h-3 w-3 text-muted" aria-hidden />{[session.connection_type, session.connection_rtt_ms != null ? `~${Math.round(session.connection_rtt_ms)} ms RTT` : '', session.connection_downlink != null ? `${Number(session.connection_downlink.toFixed(1))} Mbps` : ''].filter(Boolean).join(' · ')}</span> : 'Not captured'}</Fact><Fact label="Locale">{[session.language, session.timezone].filter(Boolean).join(' · ') || 'Not captured'}</Fact><Fact label="Display">{[session.screen_res && `screen ${session.screen_res}`, session.viewport && `viewport ${session.viewport}`].filter(Boolean).join(' · ') || 'Not captured'}</Fact><Fact label="Started">{new Date(session.started_at).toLocaleString()}</Fact><Fact label="Backend traces">{traceIds.length ? <div className="flex flex-wrap gap-2">{traceIds.map((id) => <TracePivot key={id} traceId={id} compact />)}</div> : 'No correlated trace'}</Fact></CardContent></Card>
      <div><div className="mb-3 flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-text">Session timeline</h3><p className="text-[11px] text-muted">Grouped by page view — actions, resources, errors and main-thread work are nested under the view where they happened.{totalEvents > loadedEvents ? ` Showing the first ${loadedEvents.toLocaleString()} of ${totalEvents.toLocaleString()} events.` : ''}</p></div><Badge variant="outline">{loadedEvents.toLocaleString()}{totalEvents > loadedEvents ? ` / ${totalEvents.toLocaleString()}` : ''} events</Badge></div><Timeline events={detail?.timeline ?? []} /></div>
    </div>
  )
}

function titleFor(kind: RumDetailKind, selected: Selected, detail?: RumSessionDetail, errorDetail?: RumErrorDetail): { title: string; description: string; icon: typeof Activity } {
  if (kind === 'session') {
    const sessionId = (detail?.session ?? selected as RumSession | undefined)?.session_id ?? ''
    return { title: `Session ${sessionId.length > 12 ? `${sessionId.slice(0, 8)}…` : sessionId}`, description: 'Chronological real-user journey and backend correlation.', icon: UserRound }
  }
  if (kind === 'error') return { title: (selected as RumError | undefined)?.error_type || errorDetail?.error_type || 'JavaScript error', description: 'Impact, lifecycle, symbolicated stack and what the user did before it.', icon: FileWarning }
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
  errorDetail,
  errorLoading,
  errorError,
  onRetryError,
  onIssueStatus,
  issueUpdating,
  onOpenSession,
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
  errorDetail?: RumErrorDetail
  errorLoading?: boolean
  errorError?: unknown
  onRetryError?: () => void
  onIssueStatus?: (input: { fingerprint: string; application_id: string; env: string; status: 'open' | 'resolved' | 'ignored'; note: string; release: string }) => void
  issueUpdating?: boolean
  onOpenSession?: (sessionId: string) => void
  onClose: () => void
  onDrill: (tab: 'sessions' | 'errors' | 'resources', viewName: string) => void
}) {
  const meta = titleFor(kind, selected, sessionDetail, errorDetail)
  const Icon = meta.icon
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose() }}>
      <DialogContent className="bottom-0 left-auto right-0 top-0 block h-screen w-[min(760px,calc(100vw-1rem))] max-w-none translate-x-0 translate-y-0 overflow-y-auto rounded-none border-y-0 border-r-0 p-0 sm:w-[min(760px,calc(100vw-3rem))]">
        <DialogHeader className="sticky top-0 z-10 border-b border-border bg-surface/95 px-5 py-4 pr-12 backdrop-blur">
          <DialogTitle className="flex items-center gap-2 text-base">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary/10 text-primary"><Icon className="h-4 w-4" /></span>
            <span className="min-w-0 truncate">{meta.title}</span>
          </DialogTitle>
          <DialogDescription className="pl-11 text-xs">{meta.description}</DialogDescription>
        </DialogHeader>
        <div className="p-5">
          {kind === 'session' ? <SessionDetailPanel fallback={selected as RumSession | undefined} detail={sessionDetail} loading={sessionLoading} error={sessionError} onRetry={onRetrySession} />
            : kind === 'view' && selected ? <ViewDetail view={selected as RumView} onDrill={onDrill} />
              : kind === 'error' && (selected || errorDetail || errorLoading || errorError) ? <ErrorDetail row={selected as RumError | undefined} detail={errorDetail} loading={errorLoading} loadError={errorError} onRetry={onRetryError} onIssueStatus={onIssueStatus} issueUpdating={issueUpdating} onOpenSession={onOpenSession} onDrill={onDrill} />
                : kind === 'resource' && selected ? <ResourceDetail resource={selected as RumResource} />
                  : kind === 'action' && selected ? <ActionDetail action={selected as RumAction} />
                    : <div className="py-16 text-center text-xs text-muted">This record is not on the current result page. Return to the list and open it again.</div>}
        </div>
      </DialogContent>
    </Dialog>
  )
}
