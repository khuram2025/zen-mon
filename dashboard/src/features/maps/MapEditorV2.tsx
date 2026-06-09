import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ReactFlowProvider } from '@xyflow/react'
import { useQueryClient } from '@tanstack/react-query'
import { Activity, Copy, Loader2, Pencil, Plus, Radio, Rows3 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { MapCanvas } from './canvas/MapCanvas'
import { DevicePalette } from './canvas/DevicePalette'
import { useLiveLinks, useManualMap, useManualMaps } from './useMapData'
import type { ManualMapDetail } from './core'

type Mode = 'design' | 'live'

// Stable empty reference so a disabled live query doesn't hand the canvas a new
// object every render (which would refire the edge-rebuild effect).
const EMPTY_LIVE: Record<string, never> = {}

export function MapEditorV2() {
  const [params, setParams] = useSearchParams()
  const [mode, setMode] = useState<Mode>('design')
  const [showThroughput, setShowThroughput] = useState(true)
  const [paletteOpen, setPaletteOpen] = useState(true)
  const [busy, setBusy] = useState(false)
  const qc = useQueryClient()

  const mapsQuery = useManualMaps()
  const maps = mapsQuery.data?.data || []

  const urlMapId = params.get('map')
  const selectedMapId = urlMapId || maps[0]?.id || null

  // Keep ?map in the URL once we know the default, so deep links work.
  useEffect(() => {
    if (!urlMapId && maps[0]?.id) {
      const next = new URLSearchParams(params)
      next.set('map', maps[0].id)
      setParams(next, { replace: true })
    }
  }, [urlMapId, maps, params, setParams])

  const mapQuery = useManualMap(selectedMapId)
  const detail = mapQuery.data
  const liveLinksQuery = useLiveLinks(selectedMapId, mode === 'live')
  const liveData = liveLinksQuery.data?.data || EMPTY_LIVE

  const counts = useMemo(() => ({ nodes: detail?.nodes.length || 0, links: detail?.links.length || 0 }), [detail])

  const goToMap = (id: string) => {
    const next = new URLSearchParams(params)
    next.set('map', id)
    setParams(next, { replace: false })
    setMode('design')
  }

  // New blank design.
  const newDesign = async () => {
    const name = window.prompt('Name for the new design', 'Untitled map')
    if (!name?.trim()) return
    setBusy(true)
    try {
      const { data } = await api.post('/maps', { name: name.trim() })
      await qc.invalidateQueries({ queryKey: ['manual-maps'] })
      goToMap(data.id)
      toast.success('New design created')
    } catch { toast.error('Failed to create design') } finally { setBusy(false) }
  }

  // Duplicate the current map into a new one (a "version" to modify freely).
  const duplicate = async () => {
    if (!detail) return
    const name = window.prompt('Name for the duplicate', `${detail.name} (copy)`)
    if (!name?.trim()) return
    setBusy(true)
    try {
      const newId = await cloneMap(detail, name.trim())
      await qc.invalidateQueries({ queryKey: ['manual-maps'] })
      goToMap(newId)
      toast.success('Map duplicated')
    } catch { toast.error('Duplicate failed') } finally { setBusy(false) }
  }
  const usedIds = useMemo(() => new Set((detail?.nodes || []).map((n) => n.device_id)), [detail])

  return (
    <div className="-m-5 flex h-[calc(100vh-2.75rem)] flex-col bg-bg">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-border bg-surface/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <Rows3 className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold text-text">Network Studio</span>
        </div>

        {/* Map switcher */}
        <select
          className="ml-2 max-w-[16rem] rounded-md border border-border bg-surface px-2 py-1 text-xs text-text"
          value={selectedMapId || ''}
          onChange={(e) => {
            const next = new URLSearchParams(params)
            next.set('map', e.target.value)
            setParams(next, { replace: true })
          }}
        >
          {maps.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>

        {detail && (
          <span className="text-xs text-muted">{counts.nodes} devices · {counts.links} links</span>
        )}

        <button
          type="button"
          onClick={newDesign}
          disabled={busy}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted transition hover:border-primary/50 hover:text-text disabled:opacity-50"
          title="Create a new blank design"
        >
          <Plus className="h-3.5 w-3.5" /> New design
        </button>
        <button
          type="button"
          onClick={duplicate}
          disabled={busy || !detail}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted transition hover:border-primary/50 hover:text-text disabled:opacity-50"
          title="Duplicate this map as a new version you can edit"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />} Duplicate
        </button>

        <div className="flex-1" />

        {/* Design / Live toggle */}
        <div className="flex items-center rounded-lg border border-border bg-surface p-0.5">
          <button
            type="button"
            onClick={() => { setMode('design'); mapQuery.refetch() }}
            className={cn('flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition', mode === 'design' ? 'bg-primary text-white' : 'text-muted hover:text-text')}
          >
            <Pencil className="h-3.5 w-3.5" /> Design
          </button>
          <button
            type="button"
            // Refetch so the live view reflects every saved design edit (edits
            // persist immediately but the cache isn't auto-invalidated mid-design).
            onClick={() => { setMode('live'); mapQuery.refetch() }}
            className={cn('flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition', mode === 'live' ? 'bg-success text-white' : 'text-muted hover:text-text')}
          >
            <Radio className="h-3.5 w-3.5" /> Live
          </button>
        </div>

        {mode === 'live' && (
          <button
            type="button"
            onClick={() => setShowThroughput((v) => !v)}
            className={cn('flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium transition', showThroughput ? 'border-success/40 bg-success/10 text-success' : 'border-border text-muted hover:text-text')}
            title="Toggle throughput chips"
          >
            <Activity className="h-3.5 w-3.5" /> Throughput
          </button>
        )}
      </div>

      {/* Palette + canvas */}
      <div className="flex min-h-0 flex-1">
        {mode === 'design' && (
          <DevicePalette
            open={paletteOpen}
            onToggle={() => setPaletteOpen((v) => !v)}
            usedIds={usedIds}
            disabled={!selectedMapId}
          />
        )}
        <div className="relative min-h-0 flex-1">
          {mapsQuery.isLoading || (selectedMapId && mapQuery.isLoading) ? (
            <div className="flex h-full items-center justify-center text-muted">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading map…
            </div>
          ) : !selectedMapId ? (
            <div className="flex h-full items-center justify-center text-muted">No maps yet. Create one in the classic editor.</div>
          ) : detail ? (
            <ReactFlowProvider>
              <MapCanvas
                mapId={selectedMapId}
                detail={detail}
                liveData={liveData}
                liveMode={mode === 'live'}
                showThroughput={showThroughput}
              />
            </ReactFlowProvider>
          ) : null}
        </div>
      </div>
    </div>
  )
}

/* Deep-copy a map into a brand-new one using the existing endpoints (no backend
 * change needed). Node/shape ids are server-generated, so links + annotation
 * links are remapped from old ids to new ones. */
async function cloneMap(detail: ManualMapDetail, name: string): Promise<string> {
  const { data: created } = await api.post('/maps', { name, description: detail.description || null })
  const newId: string = created.id

  const nodeMap: Record<string, string> = {}
  for (const n of detail.nodes) {
    const { data: cn } = await api.post(`/maps/${newId}/nodes`, { device_id: n.device_id, icon: n.icon || 'auto', x_pct: n.x_pct, y_pct: n.y_pct })
    nodeMap[n.id] = cn.id
    await api.put(`/maps/${newId}/nodes/${cn.id}`, { label: n.label, icon: n.icon || 'auto', metadata: n.metadata || {} })
  }
  for (const l of detail.links) {
    const s = nodeMap[l.source_node_id], t = nodeMap[l.target_node_id]
    if (!s || !t) continue
    await api.post(`/maps/${newId}/links`, { source_node_id: s, target_node_id: t, label: l.label, link_type: l.link_type, metadata: l.metadata || {} })
  }
  const shapeMap: Record<string, string> = {}
  for (const sh of detail.shapes || []) {
    const { data: cs } = await api.post(`/maps/${newId}/shapes`, {
      kind: sh.kind, x_pct: sh.x_pct, y_pct: sh.y_pct, w_pct: sh.w_pct, h_pct: sh.h_pct,
      text: sh.text, fill: sh.fill, stroke: sh.stroke, z_index: sh.z_index, metadata: sh.metadata || {},
    })
    shapeMap[sh.id] = cs.id
  }
  const srcMeta = (detail.metadata as any) || {}
  const annLinks = (srcMeta.annotation_links || []) as any[]
  const remapId = (id: string, type: string) => (type === 'shape' ? shapeMap[id] : nodeMap[id])
  const remapped = annLinks
    .map((al) => {
      const s = remapId(al.source, al.source_type), t = remapId(al.target, al.target_type)
      if (!s || !t) return null
      return { ...al, id: `al-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`, source: s, target: t }
    })
    .filter(Boolean)
  if (remapped.length) await api.put(`/maps/${newId}`, { metadata: { ...srcMeta, annotation_links: remapped } })
  return newId
}
