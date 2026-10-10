import type { ReactNode } from 'react'
import { QueryBoundary } from '@/components/ui/QueryState'

type GateQuery<T> = Parameters<typeof QueryBoundary<T>>[0]['query']

/**
 * QueryBoundary for a page body that must never render without data. QueryBoundary alone reads "not loading,
 * no error" as "data is there", which is also what a query that is waiting looks like (disabled until the
 * account's permissions have arrived, or paused offline): here that is the skeleton too.
 */
export function QueryGate<T>({
  query, what, skeleton, children,
}: {
  query: GateQuery<T>
  /** "the operating system breakdown": completes "Could not load ...". */
  what: string
  skeleton: ReactNode
  children: (data: T) => ReactNode
}) {
  if (query.data === undefined && !query.isError) return <>{skeleton}</>
  return (
    <QueryBoundary query={query} what={what} skeleton={skeleton}>
      {children}
    </QueryBoundary>
  )
}
