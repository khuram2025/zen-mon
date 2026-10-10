import { useQuery } from '@tanstack/react-query'
import { serverNow } from '@/lib/serverClock'
import { inventoryApi } from '@/pages/inventory/api'
import { useInventoryAccess } from './InventoryAccess'

/** Query key of the capture start (shared by every page that shows a software "First seen"). */
export const HISTORY_START_KEY = ['inventory', 'history-start'] as const

const HOUR = 3_600_000

/** The window of the probe: the last hour, rounded down to the hour so the request is the same all hour. */
export function historyProbeSince(now: number): string {
  return new Date(Math.floor(now / HOUR) * HOUR - HOUR).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/**
 * When change capture began on this installation (`history_start` of GET /inventory/changes; install-wide, so the
 * same for every role). A software row whose "first seen" is at or before it was already installed when Inventory
 * started looking: its date is when Inventory first SAW it, not when it was installed, and the pages say
 * "before <date>" for it (DT-05, UP-06). Only the answer's `history_start` is used, so the request asks for a
 * one-hour window and one row. `undefined` while it loads or if it cannot be read: the pages then show the dates
 * as stored rather than hold up the table.
 */
export function useSoftwareHistoryStart(): string | null | undefined {
  const access = useInventoryAccess()
  const query = useQuery({
    queryKey: HISTORY_START_KEY,
    queryFn: async () => (await inventoryApi.changes({ since: historyProbeSince(serverNow()), page_size: 1 })).history_start,
    staleTime: 30 * 60_000,
    retry: 1,
    enabled: access === 'allowed',
  })
  return query.data
}
