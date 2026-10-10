import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { inventoryAccess } from '@/components/inventory/InventoryAccess'
import { effectiveScopeTags } from '@/components/inventory/model'
import { useAuth } from '@/stores/auth'
import { inventoryApi } from './api'

export const SUMMARY_KEY = ['inventory', 'summary'] as const

/** Every cached Inventory answer except the settings form and the summary (which has just been read). */
export function isInventoryData(queryKey: readonly unknown[]): boolean {
  return queryKey[0] === 'inventory' && queryKey[1] !== 'settings' && queryKey[1] !== 'summary'
}

/** The backlog has just cleared: the last summary said normalising, this one does not. */
export function normalisingFinished(previous: boolean | undefined, next: boolean | undefined): boolean {
  return previous === true && next === false
}

/**
 * Refetches every Inventory answer the moment the normaliser has finished. Only the summary polls while the
 * backlog drains; the data pages (software, OS, hardware, assets) refetch every five minutes or never, so without
 * this they would keep showing the partial pre-normalisation figures, with the banner gone, for minutes (review
 * finding NF-04). The banner's own wording ("the figures refresh when processing finishes") relies on it.
 */
export function useRefreshWhenNormalised(normalising: boolean | undefined) {
  const client = useQueryClient()
  const previous = useRef<boolean | undefined>(undefined)
  useEffect(() => {
    if (normalisingFinished(previous.current, normalising)) refreshInventoryData(client)
    if (normalising !== undefined) previous.current = normalising
  }, [normalising, client])
}

export function refreshInventoryData(client: QueryClient) {
  void client.invalidateQueries({ predicate: (q) => isInventoryData(q.queryKey) })
}

/**
 * What the meta-line under the tabs needs: the summary (shared with the Overview through the same
 * query key, polled once a minute) and the caller's scope. Anyone holding `inventory.view` can read
 * the summary; for a scoped user the server already counts visible assets only. The request waits
 * until the account's permissions are known and holds `inventory.view`, so a role without it never
 * sends one (and a fresh sign-in does not send one that is bound to be refused).
 */
export function useInventoryMeta(enabled = true) {
  const user = useAuth((s) => s.user)
  const access = inventoryAccess(user)
  const query = useQuery({
    queryKey: SUMMARY_KEY,
    queryFn: inventoryApi.summary,
    // Quicker while the normaliser is working through a backlog, so the banner goes away when it is done.
    refetchInterval: (q) => (q.state.data?.data_status?.normalising ? 15_000 : 60_000),
    staleTime: 30_000,
    enabled: enabled && access === 'allowed',
  })
  // The server's own statement of the scope wins; before it answers, the account's tags tell the same story.
  const fromServer = query.data?.scope
  const scopeTags = fromServer ? (fromServer.restricted ? fromServer.tags : []) : effectiveScopeTags(user)
  return {
    summary: query.data,
    // Waiting for the permissions to load reads as loading, not as a failure.
    loading: query.isLoading || (enabled && access === 'unknown'),
    failed: query.isError,
    refetch: () => void query.refetch(),
    scopeTags,
    query,
  }
}
