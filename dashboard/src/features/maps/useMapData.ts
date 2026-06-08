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

/** Persist a node's new position (percent coords) — keeps x_pct/y_pct
 *  authoritative so the v1 editor keeps working unchanged. */
export function useNodeMutations(mapId: string | null) {
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['manual-map', mapId] })
  }

  const move = useMutation({
    mutationFn: async ({ id, x_pct, y_pct }: { id: string; x_pct: number; y_pct: number }) =>
      (await api.put(`/maps/${mapId}/nodes/${id}`, { x_pct, y_pct })).data,
    onSuccess: invalidate,
  })

  return { move }
}
