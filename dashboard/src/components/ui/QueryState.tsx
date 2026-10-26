import type { ReactNode } from 'react'
import { AlertTriangle, Loader2, Lock, RefreshCw, WifiOff } from 'lucide-react'
import { describeFailure } from '@/lib/access'
import { Button } from './Button'
import { Skeleton } from './Skeleton'

/** What a screen shows when its data could not be loaded: distinguishes
 *  "you may not see this" from "the appliance is unreachable" from "it failed",
 *  and never pretends the answer is an empty list. */
export function QueryFailure({ error, onRetry, retrying, compact, what = 'this data' }: {
  error: unknown
  onRetry?: () => void
  retrying?: boolean
  compact?: boolean
  what?: string
}) {
  const f = describeFailure(error)
  const Icon = f.kind === 'forbidden' ? Lock : f.kind === 'network' || f.kind === 'timeout' ? WifiOff : AlertTriangle
  const title =
    f.kind === 'forbidden' ? `Your role cannot view ${what}`
    : f.kind === 'network' ? 'Appliance unreachable'
    : f.kind === 'timeout' ? 'The request timed out'
    : `Could not load ${what}`
  return (
    <div role="alert" className={compact ? 'flex items-center gap-3 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm' : 'flex flex-col items-center gap-2 rounded-md border border-danger/30 bg-danger/5 px-6 py-8 text-center'}>
      <Icon className={compact ? 'h-4 w-4 shrink-0 text-danger' : 'h-6 w-6 text-danger'} aria-hidden />
      <div className={compact ? 'min-w-0 flex-1' : ''}>
        <div className="text-sm font-medium text-text">{title}</div>
        <div className="text-xs text-muted">{f.message}</div>
      </div>
      {onRetry && f.kind !== 'forbidden' && (
        <Button type="button" size="sm" variant="outline" onClick={onRetry} disabled={retrying}>
          {retrying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Retry
        </Button>
      )}
    </div>
  )
}

/** Renders `children` when the query has data; a skeleton while loading for the
 *  first time; an explicit failure (with retry) when it failed. Stale data stays
 *  visible with a banner when a background refresh fails. */
export function QueryBoundary<T>({ query, children, skeleton, what }: {
  query: { data: T | undefined; isLoading: boolean; isError: boolean; error: unknown; refetch: () => unknown; isFetching: boolean }
  children: (data: T) => ReactNode
  skeleton?: ReactNode
  what?: string
}) {
  if (query.isLoading) return <>{skeleton ?? <Skeleton className="h-24 w-full" />}</>
  if (query.isError && query.data === undefined) {
    return <QueryFailure error={query.error} what={what} onRetry={() => void query.refetch()} retrying={query.isFetching} />
  }
  return (
    <>
      {query.isError && (
        <div className="mb-2">
          <QueryFailure compact error={query.error} what={what} onRetry={() => void query.refetch()} retrying={query.isFetching} />
          <p className="mt-1 text-xs text-muted">Showing the last data that loaded; it may be out of date.</p>
        </div>
      )}
      {children(query.data as T)}
    </>
  )
}
