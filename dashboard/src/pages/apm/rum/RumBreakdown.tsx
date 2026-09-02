import type { ReactNode } from 'react'
import { Database, Globe2, Server, UserRound, Waypoints } from 'lucide-react'
import { cn } from '@/lib/utils'
import { RequestFlow } from '@/components/apm/explorer'
import type { RumBackendTiming, RumRequestTiming } from '@/types/apm'
import { formatDurationMs } from './model'

/**
 * Request path decomposition. A browser measurement splits into
 * network phases (Resource Timing) and, when the backend responded with
 * Server-Timing or a correlated APM trace exists, into application execution
 * and database time. `wait_ms` (request sent → first byte) contains one
 * network round trip plus the whole server think time, so network time is
 * derived as wait − server whenever a server-side figure is available.
 */
export interface PhaseSegment {
  key: string
  label: string
  ms: number
  color: string
}

export const PHASE_COLORS: Record<string, string> = {
  blocked: '#94a3b8',
  redirect: '#64748b',
  dns: '#6366f1',
  tcp: '#0284c7',
  tls: '#7c3aed',
  network: '#f59e0b',
  server: '#10b981',
  db: '#0d9488',
  download: '#38bdf8',
  processing: '#db2777',
  wait: '#f59e0b',
}

const MIN_VISIBLE_PCT = 1.2

function push(segments: PhaseSegment[], key: string, label: string, ms: number | null | undefined) {
  if (ms == null || !Number.isFinite(ms) || ms < 0.05) return
  segments.push({ key, label, ms, color: PHASE_COLORS[key] ?? '#94a3b8' })
}

/** Split one request's timing into ordered, non-overlapping phase segments. */
export function phaseSegments(timing: RumRequestTiming, backend?: RumBackendTiming | null): PhaseSegment[] {
  const segments: PhaseSegment[] = []
  push(segments, 'blocked', 'Queued', timing.blocked_ms)
  push(segments, 'redirect', 'Redirect', timing.redirect_ms)
  push(segments, 'dns', 'DNS', timing.dns_ms)
  push(segments, 'tcp', 'TCP', Math.max(0, (timing.connect_ms || 0) - (timing.tls_ms || 0)))
  push(segments, 'tls', 'TLS', timing.tls_ms)
  const serverMs = timing.has_server_timing && timing.server_ms > 0
    ? timing.server_ms
    : (backend && backend.server_ms > 0 ? backend.server_ms : 0)
  const dbMs = timing.has_server_timing && timing.db_ms > 0
    ? timing.db_ms
    : (backend && backend.db_ms > 0 ? backend.db_ms : 0)
  const wait = timing.wait_ms || 0
  if (serverMs > 0 && wait > 0) {
    push(segments, 'network', 'Network', Math.max(0, wait - Math.min(serverMs, wait)))
    push(segments, 'server', 'App', Math.max(0, Math.min(serverMs, wait) - Math.min(dbMs, serverMs)))
    push(segments, 'db', 'Database', Math.min(dbMs, serverMs))
  } else {
    push(segments, 'wait', 'Wait (network + server)', wait)
  }
  push(segments, 'download', 'Download', timing.download_ms)
  push(segments, 'processing', 'Render', timing.processing_ms)
  return segments
}

/** Horizontal stacked bar of request phases; hover each segment for details. */
export function PhaseBar({ segments, className, height = 8 }: { segments: PhaseSegment[]; className?: string; height?: number }) {
  const total = segments.reduce((sum, segment) => sum + segment.ms, 0)
  if (!segments.length || total <= 0) return null
  return (
    <div className={cn('flex w-full overflow-hidden rounded-full bg-surface2', className)} style={{ height }} role="img" aria-label={segments.map((s) => `${s.label} ${formatDurationMs(s.ms)}`).join(', ')}>
      {segments.map((segment) => (
        <div
          key={segment.key}
          title={`${segment.label} · ${formatDurationMs(segment.ms)} (${((segment.ms / total) * 100).toFixed(1)}%)`}
          style={{ width: `${Math.max((segment.ms / total) * 100, MIN_VISIBLE_PCT)}%`, background: segment.color }}
        />
      ))}
    </div>
  )
}

export function PhaseLegend({ segments, className }: { segments: PhaseSegment[]; className?: string }) {
  if (!segments.length) return null
  return (
    <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-1', className)}>
      {segments.map((segment) => (
        <span key={segment.key} className="inline-flex items-center gap-1.5 text-[10px] text-muted">
          <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: segment.color }} aria-hidden />
          {segment.label}
          <span className="font-mono tabular-nums text-text2">{formatDurationMs(segment.ms)}</span>
        </span>
      ))}
    </div>
  )
}

/**
 * Client → Network → Application → Database hop chain for one request.
 */
export function RequestPathFlow({
  timing,
  backend,
  clientHint,
  clientMetric,
  totalMs,
  status,
}: {
  timing?: RumRequestTiming | null
  backend?: RumBackendTiming | null
  clientHint?: string
  clientMetric?: string
  totalMs?: number | null
  status?: number | string | null
}) {
  const serverMs = timing?.has_server_timing && timing.server_ms > 0
    ? timing.server_ms
    : (backend && backend.server_ms > 0 ? backend.server_ms : null)
  const dbMs = timing?.has_server_timing && timing.db_ms > 0
    ? timing.db_ms
    : (backend && backend.db_ms > 0 ? backend.db_ms : null)
  const wait = timing?.wait_ms ?? null
  // When the backend figure is an average over traces it can exceed a p75
  // browser total; the network share is then not separable rather than zero.
  const inseparable = serverMs != null && wait != null && wait > 0 && serverMs >= wait
  const networkMs = serverMs != null && wait != null && wait > 0
    ? Math.max(0, wait - Math.min(serverMs, wait))
    : wait
  const hops: Parameters<typeof RequestFlow>[0]['hops'] = [
    { id: 'client', label: 'Client', hint: clientHint || 'Browser', metric: clientMetric, icon: UserRound, tone: 'ok' },
    {
      id: 'network',
      label: 'Network',
      hint: inseparable
        ? 'backend time ≥ browser total'
        : [timing?.protocol, serverMs != null ? 'wait − app time' : 'first-byte wait'].filter(Boolean).join(' · '),
      metric: inseparable ? undefined : networkMs != null ? formatDurationMs(networkMs) : undefined,
      icon: Waypoints,
      tone: 'muted',
    },
    {
      id: 'app',
      label: backend?.service || 'Application',
      hint: serverMs != null
        ? (timing?.has_server_timing ? 'Server-Timing' : `${backend?.spans ?? 0} trace spans`)
        : 'No execution telemetry',
      metric: serverMs != null ? formatDurationMs(Math.max(0, serverMs - (dbMs ?? 0))) : undefined,
      status: status ?? undefined,
      icon: Server,
      tone: backend?.has_error ? 'err' : serverMs != null ? 'ok' : 'muted',
    },
  ]
  if (dbMs != null && dbMs > 0) {
    hops.push({
      id: 'db',
      label: 'Database',
      hint: backend?.db_systems?.length ? backend.db_systems.join(', ') : (backend?.db_calls ? `${backend.db_calls} queries` : 'reported by server'),
      metric: formatDurationMs(dbMs),
      icon: Database,
      tone: 'ok',
    })
  }
  return <RequestFlow hops={hops} totalLabel={totalMs != null && totalMs > 0 ? formatDurationMs(totalMs) : undefined} />
}

/** Compact numbers line: net / app / db / download, only what is known. */
export function BreakdownInline({ timing, backend, className }: { timing?: RumRequestTiming | null; backend?: RumBackendTiming | null; className?: string }) {
  if (!timing) return null
  const serverMs = timing.has_server_timing && timing.server_ms > 0
    ? timing.server_ms
    : (backend && backend.server_ms > 0 ? backend.server_ms : null)
  const dbMs = timing.has_server_timing && timing.db_ms > 0
    ? timing.db_ms
    : (backend && backend.db_ms > 0 ? backend.db_ms : null)
  const parts: ReactNode[] = []
  const add = (label: string, ms: number | null | undefined, color: string) => {
    if (ms == null || ms <= 0) return
    parts.push(
      <span key={label} className="inline-flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} aria-hidden />
        {label} <span className="font-mono tabular-nums text-text2">{formatDurationMs(ms)}</span>
      </span>,
    )
  }
  if (serverMs != null && (timing.wait_ms || 0) > 0) {
    add('net', Math.max(0, timing.wait_ms - Math.min(serverMs, timing.wait_ms)), PHASE_COLORS.network)
    add('app', Math.max(0, serverMs - (dbMs ?? 0)), PHASE_COLORS.server)
    add('db', dbMs, PHASE_COLORS.db)
  } else {
    add('wait', timing.wait_ms, PHASE_COLORS.wait)
  }
  add('download', timing.download_ms, PHASE_COLORS.download)
  if (!parts.length) return null
  return <div className={cn('flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-muted', className)}>{parts}</div>
}

export function LocationHint({ country, timezone, clientIp }: { country?: string; timezone?: string; clientIp?: string }) {
  const isPrivate = !!clientIp && /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|fd|fe80|::1)/i.test(clientIp)
  const label = [country, timezone].filter(Boolean).join(' · ') || (isPrivate ? 'Internal network' : '')
  if (!label) return <>Not captured</>
  return (
    <span className="inline-flex items-center gap-1.5">
      <Globe2 className="h-3 w-3 text-muted" aria-hidden />
      {label}
      {isPrivate && country ? <span className="text-muted">· internal IP</span> : null}
    </span>
  )
}
