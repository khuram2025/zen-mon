import type { ReactNode } from 'react'
import { useId } from 'react'
import { Card } from '@/components/ui/Card'
import { QueryBoundary, QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

/** The frame of an Overview panel: a bordered card with a header (title, hint, controls) and a body. */
export function Panel({
  title, icon, hint, right, children, className, bodyClassName, id,
}: {
  title: string
  icon?: ReactNode
  hint?: string
  right?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
}) {
  const auto = useId()
  const headingId = `${id ?? auto}-title`
  return (
    <Card className={cn('flex min-w-0 flex-col', className)}>
      <section aria-labelledby={headingId} className="flex min-h-0 flex-1 flex-col" id={id}>
        <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 border-b border-border px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            {icon && <span className="text-muted" aria-hidden>{icon}</span>}
            <h2 id={headingId} className="text-sm font-semibold tracking-tight">{title}</h2>
            {hint && <span className="truncate text-2xs uppercase tracking-wider text-muted">{hint}</span>}
          </div>
          {right}
        </div>
        <div className={cn('min-h-0 flex-1 p-4', bodyClassName)}>{children}</div>
      </section>
    </Card>
  )
}

/** Placeholder rows that keep a panel's height while it loads (the layout never shifts). */
export function PanelSkeleton({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-2.5', className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={cn('h-5', i % 3 === 1 ? 'w-5/6' : i % 3 === 2 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  )
}

/**
 * A panel whose data comes from its own query: skeleton while it loads, its own error state with a
 * retry (the rest of the page still renders), and the last data with a banner when a refresh fails.
 */
export function QueryPanel<T>({
  title, icon, hint, right, query, what, rows, children, className, bodyClassName, id, quietStale,
}: {
  title: string
  icon?: ReactNode
  hint?: string
  right?: ReactNode
  query: Parameters<typeof QueryBoundary<T>>[0]['query']
  /** "the operating system breakdown": completes "Could not load ...". */
  what: string
  rows?: number
  children: (data: T) => ReactNode
  className?: string
  bodyClassName?: string
  id?: string
  /**
   * Panels fed by the summary share one request, so a failed refresh would put the same "showing the last data"
   * banner in each of them: they stay quiet and the page says it once (the KPI row, the meta-line under the tabs).
   * A failure with no data at all is still shown in the panel.
   */
  quietStale?: boolean
}) {
  return (
    <Panel title={title} icon={icon} hint={hint} right={right} className={className} bodyClassName={bodyClassName} id={id}>
      {query.data === undefined && !query.isError ? (
        // Loading, or waiting (offline, or the account's permissions have not arrived): never a render without data.
        <PanelSkeleton rows={rows} />
      ) : quietStale ? (
        query.isError && query.data === undefined ? (
          <QueryFailure compact error={query.error} what={what} onRetry={() => void query.refetch()} retrying={query.isFetching} />
        ) : (
          children(query.data as T)
        )
      ) : (
        <QueryBoundary query={query} what={what} skeleton={<PanelSkeleton rows={rows} />}>
          {children}
        </QueryBoundary>
      )}
    </Panel>
  )
}
