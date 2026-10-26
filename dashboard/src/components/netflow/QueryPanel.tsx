import type { ReactNode } from 'react'
import { AlertTriangle, Archive, CircleSlash, DatabaseZap, Loader2, Lock, RefreshCw, SearchX, type LucideIcon } from 'lucide-react'
import { Skeleton } from '@/components/ui/Skeleton'
import { useServerNowTick } from '@/components/services/detail/clock'
import { clockOffsetMs } from '@/lib/serverClock'
import { cn, relativeTime } from '@/lib/utils'
import { formatDateTime, zonedDate } from '@/lib/netflow/format'
import {
  apiErrorMessage,
  apiErrorRef,
  panelStatus,
  placeholderStatus,
  retentionWarnings,
  type PanelQueryLike,
  type PanelStatusOptions,
} from '@/lib/netflow/status'
import { displayTimeZone } from '@/lib/netflow/window'

type SkeletonKind = 'rows' | 'chart' | 'tiles' | 'block'

export interface QueryPanelProps<D> extends PanelStatusOptions<D> {
  /** Any TanStack query (or a structurally compatible object). */
  query: PanelQueryLike<D>
  /** Rendered for real data (ok, stale, or a previous result while the next one loads). */
  children: (data: D) => ReactNode
  /** What the panel shows, for messages and screen readers ("top talkers"). */
  label?: string
  /** Title for a successful query that matched nothing (default: no flows matched). */
  empty?: ReactNode
  /** Explanation for a window with no records at all. */
  noCoverage?: ReactNode
  skeleton?: SkeletonKind | ReactNode
  /** Tighter spacing for small cards. */
  compact?: boolean
  className?: string
}

/**
 * The one place a NetFlow panel decides what to show (NF-03). Loading, failed, forbidden,
 * uncovered, unsupported and empty never render as data; a failed refresh keeps the last good
 * data on screen with the time it was fetched; a new window or filter keeps the previous chart
 * dimmed (not replaced by zeros) until its own answer arrives.
 */
export function QueryPanel<D>({
  query,
  children,
  label = 'this data',
  empty,
  noCoverage,
  skeleton = 'block',
  compact,
  className,
  ...statusOpts
}: QueryPanelProps<D>) {
  const status = panelStatus(query, statusOpts)
  const pad = compact ? 'py-6' : 'py-10'

  switch (status) {
    case 'loading': {
      if (query.data !== undefined && placeholderStatus(query, statusOpts) === 'ok') {
        return (
          <div className={cn('relative', className)} aria-busy="true">
            <div className="pointer-events-none select-none opacity-50 transition-opacity">{children(query.data)}</div>
            <div className="absolute right-2 top-2">
              <span role="status" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-text2 shadow-card">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                Loading the new selection…
              </span>
            </div>
          </div>
        )
      }
      return (
        <div role="status" aria-busy="true" className={className}>
          <span className="sr-only">Loading {label}…</span>
          {isSkeletonKind(skeleton) ? <PanelSkeleton kind={skeleton} /> : skeleton}
        </div>
      )
    }
    case 'forbidden':
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={Lock}
          title={`You do not have access to ${label}`}
          detail="Your role or exporter scope does not include it. An administrator can grant NetFlow access or widen your scope."
        />
      )
    case 'error':
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={AlertTriangle}
          iconClass="text-danger-fg"
          title={`Could not load ${label}`}
          detail={apiErrorMessage(query.error)}
          errorRef={apiErrorRef(query.error)}
          onRetry={query.refetch}
          retrying={query.isFetching}
        />
      )
    case 'stale':
      return (
        <div className={cn('space-y-2', className)}>
          <StaleChip query={query} />
          {children(query.data as D)}
        </div>
      )
    case 'no_coverage':
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={DatabaseZap}
          iconClass="text-warning-fg"
          title="No flow records were received for this window"
          detail={
            noCoverage ??
            'The exporters in scope delivered nothing in this period, or it is outside the retention period. Traffic is unknown here, not zero.'
          }
        />
      )
    case 'expired': {
      const reasons = retentionWarnings(query.data)
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={Archive}
          iconClass="text-warning-fg"
          title="This window is older than the data kept for this query"
          detail={`${reasons.length ? reasons.join(' ') : 'Nothing retained covers this window for these filters.'} Traffic is unknown here, not zero.`}
        />
      )
    }
    case 'unsupported':
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={CircleSlash}
          title={`${capitalize(label)} is not available`}
          detail={typeof statusOpts.unsupported === 'string' ? statusOpts.unsupported : 'The exporters in scope do not provide the fields this panel needs.'}
        />
      )
    case 'empty':
      return (
        <PanelMessage
          className={cn(pad, className)}
          icon={SearchX}
          title={empty ?? 'No flows matched this window and filters'}
        />
      )
    case 'ok':
      return <div className={className}>{children(query.data as D)}</div>
  }
}

function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s
}

function isSkeletonKind(v: unknown): v is SkeletonKind {
  return v === 'rows' || v === 'chart' || v === 'tiles' || v === 'block'
}

function PanelSkeleton({ kind }: { kind: SkeletonKind }) {
  if (kind === 'rows') {
    return (
      <div className="space-y-2 py-2">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex gap-3">
            <Skeleton className="h-5 flex-[2]" />
            <Skeleton className="h-5 flex-1" />
            <Skeleton className="h-5 flex-1" />
          </div>
        ))}
      </div>
    )
  }
  if (kind === 'tiles') {
    return (
      <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-[84px]" />
        ))}
      </div>
    )
  }
  if (kind === 'chart') {
    return (
      <div className="flex h-56 items-end gap-1.5 py-2">
        {[40, 65, 50, 80, 55, 70, 45, 90, 60, 75, 50, 65].map((h, i) => (
          <div key={i} className="animate-shimmer flex-1 rounded-md" style={{ height: `${h}%` }} />
        ))}
      </div>
    )
  }
  return <Skeleton className="h-40 w-full" />
}

interface PanelMessageProps {
  icon: LucideIcon
  iconClass?: string
  title: ReactNode
  detail?: ReactNode
  errorRef?: string | null
  onRetry?: () => unknown
  retrying?: boolean
  className?: string
}

export function PanelMessage({ icon: Icon, iconClass, title, detail, errorRef, onRetry, retrying, className }: PanelMessageProps) {
  return (
    <div role="status" className={cn('flex flex-col items-center gap-2 px-4 text-center', className)}>
      <Icon className={cn('h-6 w-6', iconClass ?? 'text-text2')} aria-hidden />
      <div className="text-sm font-medium text-text">{title}</div>
      {detail && <div className="max-w-md text-xs leading-relaxed text-text2">{detail}</div>}
      {errorRef && (
        <div className="text-xs text-text2">
          Reference <span className="select-all font-mono">{errorRef}</span>
        </div>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={() => onRetry()}
          disabled={retrying}
          className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2 disabled:opacity-60"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', retrying && 'animate-spin')} aria-hidden />
          {retrying ? 'Retrying…' : 'Retry'}
        </button>
      )}
    </div>
  )
}

/**
 * "Refresh failed, showing data from 14:03:22 (3m ago)" with Retry. The time is the appliance
 * time of the last successful fetch (TanStack stamps it with the browser clock; the calibrated
 * offset converts it), in the viewer's display timezone.
 */
export function StaleChip({ query, className }: { query: PanelQueryLike<unknown>; className?: string }) {
  const now = useServerNowTick(30_000)
  const updated = query.dataUpdatedAt ? query.dataUpdatedAt + clockOffsetMs() : null
  const tz = displayTimeZone()
  const when = updated
    ? formatDateTime(updated, tz, { zone: 'offset', timeOnly: zonedDate(updated, tz) === zonedDate(now, tz) })
    : 'an earlier refresh'
  const ref = apiErrorRef(query.error)
  return (
    <div
      role="status"
      className={cn(
        'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-warning/35 bg-warning/10 px-2.5 py-1.5 text-xs text-text',
        className,
      )}
      title={apiErrorMessage(query.error) + (ref ? ` (reference ${ref})` : '')}
    >
      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
      <span className="min-w-0">
        <span className="font-semibold text-warning-fg">Refresh failed</span>, showing data from {when}
        {updated ? ` (${relativeTime(new Date(updated).toISOString())})` : ''}
      </span>
      {query.refetch && (
        <button
          type="button"
          onClick={() => query.refetch?.()}
          disabled={query.isFetching}
          className="ml-auto inline-flex items-center gap-1 font-semibold text-text underline-offset-2 hover:underline disabled:opacity-60"
        >
          <RefreshCw className={cn('h-3 w-3', query.isFetching && 'animate-spin')} aria-hidden />
          Retry
        </button>
      )}
    </div>
  )
}
