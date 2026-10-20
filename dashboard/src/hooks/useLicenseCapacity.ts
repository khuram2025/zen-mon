import { useQuery, type QueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'

/** GET /subscription/capacity — server/app/services/license_capacity.py.
 * One node = one device or one server; a server linked to a device counts once
 * and controller-managed children never count. */
export type LicenseCapacityState =
  | 'ok' | 'near_limit' | 'full' | 'over_limit' | 'expired' | 'inactive' | 'unmetered'

export interface LicenseCapacity {
  state: LicenseCapacityState
  source: 'license' | 'evaluation' | 'unmetered'
  enforced: boolean
  can_add: boolean
  limit: number | null
  used: number
  available: number | null
  usage: {
    devices: number
    servers: number
    linked_servers: number
    controller_children: number
    nodes: number
  }
  pool_total: number | null
  pool_other_usage: number | null
  /** Subscription-wide usage now: other appliances (last check-in) + this one (live). */
  pool_used: number | null
  pool_available: number | null
  plan: string | null
  expires_at: string | null
  snapshot_at: string | null
  message: string
}

export const LICENSE_CAPACITY_KEY = ['license-capacity'] as const

export function useLicenseCapacity(enabled = true) {
  return useQuery<LicenseCapacity>({
    queryKey: LICENSE_CAPACITY_KEY,
    queryFn: async () => (await api.get<LicenseCapacity>('/subscription/capacity')).data,
    enabled,
    refetchInterval: 60_000,
    staleTime: 15_000,
  })
}

/** Call after anything that adds or removes devices or servers. */
export function invalidateLicenseCapacity(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: LICENSE_CAPACITY_KEY })
}

/** True for the 402 the API returns once the license is full. */
export function isLicenseCapacityError(e: any): boolean {
  return e?.response?.status === 402 && e?.response?.data?.detail?.code === 'license_capacity_reached'
}

/** Nodes that cannot be added right now: full, over the limit, expired or inactive. */
export function isBlocked(cap: LicenseCapacity | undefined): boolean {
  return !!cap && cap.enforced && !cap.can_add
}
