/* React Query data layer for the v2 map editor. Talks to the exact same
 * /maps endpoints as v1 so both editors share one backend + cache. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type {
  AnnotationLink,
  Device,
  LiveLinkData,
  ManualMapDetail,
  ManualMapListItem,
  NodeLiveData,
  SuggestedLink,
} from './core'

export function useManualMaps() {
  return useQuery<{ data: ManualMapListItem[] }>({
    queryKey: ['manual-maps'],
    queryFn: async () => (await api.get('/maps')).data,
  })
}

export function useManualMap(mapId: string | null, live = false) {
  return useQuery<ManualMapDetail>({
    queryKey: ['manual-map', mapId],
    enabled: !!mapId,
    // In live (NOC) mode refresh the map itself too, so node status changes
    // and topology edits land on the wall without a manual reload. Design
    // mode never auto-refetches — it would clobber in-progress edits.
    refetchInterval: live ? 30_000 : false,
    queryFn: async () => (await api.get(`/maps/${mapId}`)).data,
  })
}

const PAGE = 200 // the devices API caps `limit` at 200
const MAX_PAGES = 100

/** Every monitored device, paged through the API (it caps a page at 200), so
 *  large estates can place device #201 and beyond. */
export function useDevices() {
  return useQuery<{ data: Device[] }>({
    queryKey: ['devices', 'manual-map-picker'],
    queryFn: async () => {
      const all: Device[] = []
      for (let page = 0; page < MAX_PAGES; page++) {
        // The devices API pages with skip/limit (an unknown `offset` is ignored).
        const { data } = await api.get('/devices', { params: { limit: PAGE, skip: page * PAGE } })
        const rows: Device[] = data?.data || []
        // Guard against a backend that ignores paging: stop on a repeated page.
        if (rows.length && all.some((d) => d.id === rows[0].id)) break
        all.push(...rows)
        if (rows.length < PAGE) break
      }
      return { data: all.map((d) => ({ ...d, kind: 'device' as const })) }
    },
  })
}

/** Servers (Servers module) that can be placed as live map nodes. Fails soft
 *  for users without servers.view. */
export function useMapServers(enabled = true) {
  return useQuery<{ data: Device[] }>({
    queryKey: ['servers', 'manual-map-picker'],
    enabled,
    retry: false,
    queryFn: fetchMapServers,
  })
}

export async function fetchMapServers(): Promise<{ data: Device[] }> {
  try {
    const all: Device[] = []
    for (let page = 0; page < MAX_PAGES; page++) {
      const { data } = await api.get('/servers', { params: { page: page + 1, page_size: PAGE } })
      const rows: any[] = data?.items || data?.data || []
      all.push(...rows.map((s) => ({
        id: s.id,
        kind: 'server' as const,
        hostname: s.display_name || s.hostname || s.fqdn || 'server',
        ip_address: s.primary_ip || '',
        device_type: 'server',
        status: SERVER_STATUS[String(s.status)] || 'unknown',
        location: s.site_name || null,
        vendor: null,
        model: s.os_name || null,
      })))
      if (rows.length < PAGE) break
    }
    return { data: all }
  } catch {
    return { data: [] }
  }
}

const SERVER_STATUS: Record<string, string> = {
  healthy: 'up', warning: 'degraded', critical: 'down', disabled: 'maintenance', stale: 'unknown', unknown: 'unknown',
}

/** Service checks that can be placed as live map nodes. */
export function useMapServiceChecks(enabled = true) {
  return useQuery<{ data: Device[] }>({
    queryKey: ['service-checks', 'manual-map-picker'],
    enabled,
    retry: false,
    queryFn: fetchMapServiceChecks,
  })
}

export async function fetchMapServiceChecks(): Promise<{ data: Device[] }> {
  try {
    const rows: any[] = []
    for (let page = 0; page < MAX_PAGES; page++) {
      const { data } = await api.get('/service-checks', { params: { skip: page * PAGE, limit: PAGE } })
      const batch: any[] = Array.isArray(data) ? data : (data?.data || data?.items || [])
      rows.push(...batch)
      if (batch.length < PAGE) break
    }
    return {
      data: rows.map((c) => ({
        id: c.id,
        kind: 'service_check' as const,
        hostname: c.name,
        ip_address: c.target_host || '',
        device_type: 'service_check',
        status: c.status === 'warning' ? 'degraded' : (c.status || 'unknown'),
        location: null,
        vendor: null,
        model: c.check_type || null,
      })),
    }
  } catch {
    return { data: [] }
  }
}

export function useLiveLinks(mapId: string | null, enabled: boolean) {
  return useQuery<{ data: Record<string, LiveLinkData> }>({
    queryKey: ['manual-map-live', mapId],
    enabled: !!mapId && enabled,
    refetchInterval: enabled ? 15_000 : false,
    queryFn: async () => (await api.get(`/maps/${mapId}/links-live`)).data,
  })
}

/** Per-device live health (status, cpu/mem, alerts) for the NOC overlay.
 *  Fails soft: an older backend without the endpoint just yields no overlay. */
export function useNodesLive(mapId: string | null, enabled: boolean) {
  return useQuery<{ data: Record<string, NodeLiveData> }>({
    queryKey: ['manual-map-nodes-live', mapId],
    enabled: !!mapId && enabled,
    refetchInterval: enabled ? 15_000 : false,
    retry: false,
    queryFn: async () => {
      try {
        return (await api.get(`/maps/${mapId}/nodes-live`)).data
      } catch {
        return { data: {} }
      }
    },
  })
}

/** Discovered CDP/LLDP adjacencies among placed devices that have no cable
 *  yet. `includeStale` also returns old or never-up evidence, flagged. */
export function useSuggestedLinks(mapId: string | null, enabled: boolean, includeStale = false) {
  return useQuery<{ data: SuggestedLink[]; count: number; stale_count?: number }>({
    queryKey: ['manual-map-suggested', mapId, includeStale],
    enabled: !!mapId && enabled,
    queryFn: async () => (await api.get(`/maps/${mapId}/suggested-links`, { params: { include_stale: includeStale ? 1 : 0 } })).data,
  })
}

export type BulkNode = { client_id: string; device_id?: string | null; server_id?: string | null; service_check_id?: string | null; label?: string | null; icon?: string; x_pct: number; y_pct: number; metadata?: Record<string, unknown> }
export type BulkShape = { client_id: string; kind: string; x_pct: number; y_pct: number; w_pct: number; h_pct: number; text?: string | null; fill?: string | null; stroke?: string | null; z_index?: number; metadata?: Record<string, unknown> }
export type BulkLink = { source: string; target: string; label?: string | null; link_type?: string; metadata?: Record<string, unknown> }
export type BulkPayload = { nodes?: BulkNode[]; shapes?: BulkShape[]; links?: BulkLink[]; annotation_links?: Partial<AnnotationLink>[] }
export type BulkResult = { id_map: Record<string, string>; link_ids: string[]; annotation_link_ids: string[] }
export type BulkDelete = { node_ids?: string[]; shape_ids?: string[]; link_ids?: string[]; annotation_link_ids?: string[] }

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

  /** Place a device, server or service check on the map (structural →
   *  invalidates so it renders). label/metadata are passed through so
   *  duplicates keep their styling. */
  const addNode = useMutation({
    mutationFn: async (payload: {
      device_id?: string | null
      server_id?: string | null
      service_check_id?: string | null
      x_pct: number
      y_pct: number
      icon?: string
      label?: string | null
      metadata?: Record<string, unknown>
    }) => (await api.post(`/maps/${mapId}/nodes`, { icon: 'auto', ...payload })).data as { id: string },
    onSuccess: invalidate,
  })

  /** Create a link between two nodes (structural → invalidates so it renders). */
  const addLink = useMutation({
    mutationFn: async (payload: {
      source_node_id: string
      target_node_id: string
      label?: string | null
      link_type?: string
      metadata?: Record<string, unknown>
    }) => (await api.post(`/maps/${mapId}/links`, payload)).data as { id: string },
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

  /* ── Annotation shapes (icons / images / text / boxes) ─────────────── */
  const addShape = useMutation({
    mutationFn: async (payload: Record<string, unknown>) =>
      (await api.post(`/maps/${mapId}/shapes`, payload)).data,
    onSuccess: invalidate,
  })

  // Optimistic — no refetch, so live drag/resize/edit isn't clobbered mid-gesture.
  const updateShape = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      (await api.put(`/maps/${mapId}/shapes/${id}`, patch)).data,
  })

  const deleteShape = useMutation({
    mutationFn: async (id: string) => api.delete(`/maps/${mapId}/shapes/${id}`),
    onSuccess: invalidate,
  })

  /** Create links for discovered CDP/LLDP adjacencies among placed devices.
   *  `pairs` limits it to the adjacencies the operator ticked in the review;
   *  stale evidence is skipped unless `include_stale`. */
  const autoConnect = useMutation({
    mutationFn: async (body?: { pairs?: [string, string][]; include_stale?: boolean }) =>
      (await api.post(`/maps/${mapId}/auto-connect`, body ?? {})).data as { created: number; created_ids?: string[]; suggested: number; skipped_stale?: number },
    onSuccess: () => {
      invalidate()
      qc.invalidateQueries({ queryKey: ['manual-map-suggested', mapId] })
    },
  })

  /* Annotation links (cables touching an icon/image) live in the map's
   * metadata. Each edit is its own atomic, row-locked request so two editors
   * never overwrite each other's cables. Optimistic — no refetch. */
  const addAnnLink = useMutation({
    mutationFn: async (al: AnnotationLink) => (await api.post(`/maps/${mapId}/annotation-links`, al)).data as AnnotationLink,
  })
  const updateAnnLink = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<AnnotationLink> }) =>
      (await api.put(`/maps/${mapId}/annotation-links/${encodeURIComponent(id)}`, patch)).data as AnnotationLink,
  })
  const deleteAnnLink = useMutation({
    mutationFn: async (id: string) => api.delete(`/maps/${mapId}/annotation-links/${encodeURIComponent(id)}`),
  })

  /** Map-level UI metadata (background, legend state…). The payload never
   *  carries annotation_links, so the server keeps the stored cables. */
  const saveMapMeta = useMutation({
    mutationFn: async (metadata: Record<string, unknown>) => {
      const { annotation_links: _drop, ...rest } = metadata
      return (await api.put(`/maps/${mapId}`, { metadata: rest })).data
    },
  })

  /** Create many items in one transaction (paste, import, undo of a delete). */
  const bulkCreate = useMutation({
    mutationFn: async (payload: BulkPayload) => (await api.post(`/maps/${mapId}/bulk`, payload)).data as BulkResult,
    onSuccess: invalidate,
  })

  /** Delete many items in one transaction (multi-delete, undo of a create). */
  const bulkDelete = useMutation({
    mutationFn: async (payload: BulkDelete) => (await api.post(`/maps/${mapId}/bulk-delete`, payload)).data,
    onSuccess: invalidate,
  })

  /** Place a device's unplaced CDP/LLDP neighbours around it. */
  const expandNode = useMutation({
    mutationFn: async (nodeId: string) =>
      (await api.post(`/maps/${mapId}/nodes/${nodeId}/expand`, { include_unmanaged: true })).data as {
        placed_node_ids: string[]; link_ids: string[]; unmanaged_shape_ids: string[]; annotation_link_ids: string[]
      },
    onSuccess: invalidate,
  })

  return {
    move, bulkMove, deleteNode, deleteLink, updateNode, updateLink, addLink, addNode, addShape, updateShape, deleteShape,
    saveMapMeta, autoConnect, addAnnLink, updateAnnLink, deleteAnnLink, bulkCreate, bulkDelete, expandNode, invalidate,
  }
}
