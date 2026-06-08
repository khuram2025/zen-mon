/* React Query data layer for the v2 map editor. Talks to the exact same
 * /maps endpoints as v1 so both editors share one backend + cache. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type {
  Device,
  LiveLinkData,
  ManualMapDetail,
  ManualMapListItem,
  SuggestedLink,
} from './core'

export function useManualMaps() {
  return useQuery<{ data: ManualMapListItem[] }>({
    queryKey: ['manual-maps'],
    queryFn: async () => (await api.get('/maps')).data,
  })
}

export function useManualMap(mapId: string | null) {
  return useQuery<ManualMapDetail>({
    queryKey: ['manual-map', mapId],
    enabled: !!mapId,
    queryFn: async () => (await api.get(`/maps/${mapId}`)).data,
  })
}

export function useDevices() {
  return useQuery<{ data: Device[] }>({
    queryKey: ['devices', 'manual-map-picker'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
  })
}

export function useLiveLinks(mapId: string | null, enabled: boolean) {
  return useQuery<{ data: Record<string, LiveLinkData> }>({
    queryKey: ['manual-map-live', mapId],
    enabled: !!mapId && enabled,
    refetchInterval: enabled ? 15_000 : false,
    queryFn: async () => (await api.get(`/maps/${mapId}/links-live`)).data,
  })
}

export function useSuggestedLinks(mapId: string | null, enabled: boolean) {
  return useQuery<{ data: SuggestedLink[]; count: number }>({
    queryKey: ['manual-map-suggested', mapId],
    enabled: !!mapId && enabled,
    queryFn: async () => (await api.get(`/maps/${mapId}/suggested-links`)).data,
  })
}

/** Node/link mutations for the v2 editor. Positions stay in percent coords so
 *  x_pct/y_pct remain authoritative and the v1 editor keeps working. */
export function useMapMutations(mapId: string | null) {
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['manual-map', mapId] })
  }

  const move = useMutation({
    mutationFn: async ({ id, x_pct, y_pct }: { id: string; x_pct: number; y_pct: number }) =>
      (await api.put(`/maps/${mapId}/nodes/${id}`, { x_pct, y_pct })).data,
  })

  /** Persist many node positions at once (group move / align / snap).
   *  Intentionally does NOT invalidate — local React Flow state is already
   *  correct, so we avoid a refetch that would rebuild nodes and drop the
   *  current selection. */
  const bulkMove = useMutation({
    mutationFn: async (items: { id: string; x_pct: number; y_pct: number }[]) => {
      await Promise.all(items.map((it) => api.put(`/maps/${mapId}/nodes/${it.id}`, { x_pct: it.x_pct, y_pct: it.y_pct })))
    },
  })

  const deleteNode = useMutation({
    mutationFn: async (id: string) => api.delete(`/maps/${mapId}/nodes/${id}`),
    onSuccess: invalidate,
  })

  const deleteLink = useMutation({
    mutationFn: async (id: string) => api.delete(`/maps/${mapId}/links/${id}`),
    onSuccess: invalidate,
  })

  /** Patch a node (label, icon, metadata such as label offset). */
  const updateNode = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      (await api.put(`/maps/${mapId}/nodes/${id}`, patch)).data,
  })

  /** Patch a link (label, metadata: shape/waypoints/style). Optimistic — no
   *  refetch, so live waypoint edits aren't clobbered mid-drag. */
  const updateLink = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      (await api.put(`/maps/${mapId}/links/${id}`, patch)).data,
  })

  return { move, bulkMove, deleteNode, deleteLink, updateNode, updateLink }
}
