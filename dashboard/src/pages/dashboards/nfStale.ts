import { panelStatus, type PanelQueryLike, type PanelStatus } from '@/lib/netflow/status'

/**
 * NetFlow panels on the team dashboards (NF-03): a refresh that failed keeps the last good data,
 * and says so with its time and a Retry (the same StaleChip the NetFlow pages use) instead of a
 * LIVE badge and silently blanked figures.
 */

/** Whether a panel has data to draw: fresh, or kept after a failed refresh (then with a stale chip). */
export function hasNfData(state: PanelStatus): boolean {
  return state === 'ok' || state === 'stale'
}

/**
 * The stale queries among `queries` as one query for StaleChip: the oldest successful fetch time,
 * the first error, and a Retry that re-runs every stale one. Null when none is stale.
 */
export function combinedStale(queries: PanelQueryLike<unknown>[]): PanelQueryLike<unknown> | null {
  const stale = queries.filter((q) => panelStatus(q) === 'stale')
  if (!stale.length) return null
  const times = stale.map((q) => q.dataUpdatedAt ?? 0).filter((t) => t > 0)
  return {
    data: stale[0].data,
    error: stale[0].error,
    status: 'error',
    isFetching: stale.some((q) => !!q.isFetching),
    dataUpdatedAt: times.length ? Math.min(...times) : undefined,
    refetch: () => Promise.all(stale.map((q) => q.refetch?.())),
  }
}
