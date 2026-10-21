import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ReactFlowProvider } from '@xyflow/react'
import { useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, Check, ChevronDown, Copy, FileUp, History, Keyboard, Loader2, Map as MapIcon, Maximize2, Minimize2,
  MoreHorizontal, Pencil, Play, Plus, Radio, Rows3, Settings2, ShieldCheck, Tag, Trash2, Wand2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { MapCanvas, type MapCanvasActions } from './canvas/MapCanvas'
import { exportMenuItems } from './canvas/exportMenu'
import { VersionsDialog } from './canvas/VersionsDialog'
import { BuildMapDialog } from './canvas/BuildMapDialog'
import { parseMapJson, pickTextFile, toBulkPayload, type MapBackup } from './export/exportMap'
import { DevicePalette } from './canvas/DevicePalette'
import { ContextMenu, type ContextMenuState, type MenuItem } from './canvas/ContextMenu'
import type { ShapeSpec } from './canvas/Annotations'
import { fetchMapServers, fetchMapServiceChecks, useDevices, useLiveLinks, useManualMap, useManualMaps, useNodesLive } from './useMapData'
import type { ManualMapDetail, ManualMapListItem } from './core'

type Mode = 'design' | 'live'

// Stable empty reference so a disabled live query doesn't hand the canvas a new
// object every render (which would refire the edge-rebuild effect).
const EMPTY_LIVE: Record<string, never> = {}
const LAST_MANUAL_MAP_KEY = 'zp-manual-map-id'
const LIVE_PREFS_KEY = 'zp-map-live-prefs'

type LivePrefs = { throughput: boolean; ifaceLabels: boolean; animate: boolean; problems: boolean }
const DEFAULT_LIVE_PREFS: LivePrefs = { throughput: true, ifaceLabels: true, animate: true, problems: true }

function loadLivePrefs(): LivePrefs {
  try {
    const raw = localStorage.getItem(LIVE_PREFS_KEY)
    return raw ? { ...DEFAULT_LIVE_PREFS, ...JSON.parse(raw) } : DEFAULT_LIVE_PREFS
  } catch { return DEFAULT_LIVE_PREFS }
}

function newestCreatedMapId(maps: { id: string; created_at?: string | null; updated_at?: string | null }[]): string | null {
  if (maps.length === 0) return null
  const sorted = [...maps].sort((a, b) => {
    const at = Date.parse(a.created_at || a.updated_at || '') || 0
    const bt = Date.parse(b.created_at || b.updated_at || '') || 0
    return bt - at
  })
  return sorted[0]?.id || maps[0].id
}

export function MapEditorV2() {
  const [params, setParams] = useSearchParams()
  const [mode, setMode] = useState<Mode>(() => (params.get('mode') === 'live' ? 'live' : 'design'))
  const [livePrefs, setLivePrefs] = useState<LivePrefs>(loadLivePrefs)
  const [paletteOpen, setPaletteOpen] = useState(true)
  const [busy, setBusy] = useState(false)
  const [mapMenu, setMapMenu] = useState<ContextMenuState>(null)
  const [nameDialog, setNameDialog] = useState<{ kind: 'new' | 'duplicate' | 'rename'; name: string; description: string } | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const insertRef = useRef<((spec: ShapeSpec) => void) | null>(null)
  const canvasRef = useRef<MapCanvasActions | null>(null)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [buildOpen, setBuildOpen] = useState(false)
  const devicesQ = useDevices()
  const qc = useQueryClient()

  const mapsQuery = useManualMaps()
  const maps = mapsQuery.data?.data || []

  const urlMapId = params.get('map')
  const selectedMapId = useMemo(() => {
    if (maps.length === 0) return null
    const validIds = new Set(maps.map((m) => m.id))
    if (urlMapId && validIds.has(urlMapId)) return urlMapId
    try {
      const last = localStorage.getItem(LAST_MANUAL_MAP_KEY)
      if (last && validIds.has(last)) return last
    } catch {
      // Ignore private-mode/storage failures; URL selection still works.
    }
    return newestCreatedMapId(maps)
  }, [maps, urlMapId])

  // Keep ?map (and ?mode) in the URL so deep links reopen the same view — a
  // NOC wall can bookmark `?map=…&mode=live`.
  useEffect(() => {
    if (!selectedMapId) return
    try { localStorage.setItem(LAST_MANUAL_MAP_KEY, selectedMapId) } catch { /* best effort */ }
    const next = new URLSearchParams(params)
    let changed = false
    if (urlMapId !== selectedMapId) { next.set('map', selectedMapId); changed = true }
    const urlMode = params.get('mode')
    if (mode === 'live' && urlMode !== 'live') { next.set('mode', 'live'); changed = true }
    if (mode === 'design' && urlMode) { next.delete('mode'); changed = true }
    if (changed) setParams(next, { replace: true })
  }, [urlMapId, selectedMapId, params, setParams, mode])

  useEffect(() => {
    try { localStorage.setItem(LIVE_PREFS_KEY, JSON.stringify(livePrefs)) } catch { /* best effort */ }
  }, [livePrefs])

  useEffect(() => {
    const onFs = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFs)
    return () => document.removeEventListener('fullscreenchange', onFs)
  }, [])

  const mapQuery = useManualMap(selectedMapId, mode === 'live')
  const detail = mapQuery.data
  const liveLinksQuery = useLiveLinks(selectedMapId, mode === 'live')
  const liveData = liveLinksQuery.data?.data || EMPTY_LIVE
  const nodesLiveQuery = useNodesLive(selectedMapId, mode === 'live')
  const nodesLive = nodesLiveQuery.data?.data || EMPTY_LIVE
  const current = maps.find((m) => m.id === selectedMapId)

  const counts = useMemo(() => {
    const ann = Array.isArray(detail?.metadata?.annotation_links) ? detail!.metadata!.annotation_links!.length : 0
    const nodes = detail?.nodes || []
    const servers = nodes.filter((n) => n.kind === 'server').length
    const checks = nodes.filter((n) => n.kind === 'service_check').length
    return { devices: nodes.length - servers - checks, servers, checks, links: (detail?.links.length || 0) + ann, shapes: detail?.shapes?.length || 0 }
  }, [detail])

  const goToMap = (id: string, nextMode: Mode = 'design') => {
    const next = new URLSearchParams(params)
    next.set('map', id)
    if (nextMode === 'live') next.set('mode', 'live'); else next.delete('mode')
    setParams(next, { replace: false })
    setMode(nextMode)
  }

  const switchMode = (m: Mode) => {
    setMode(m)
    // Edits persist immediately but the cache isn't auto-invalidated mid-design.
    mapQuery.refetch()
  }

  /* ── Map CRUD ─────────────────────────────────────────────────────────── */
  const submitName = async () => {
    if (!nameDialog) return
    const name = nameDialog.name.trim()
    if (!name) { toast.error('Give the map a name'); return }
    setBusy(true)
    try {
      if (nameDialog.kind === 'new') {
        const { data } = await api.post('/maps', { name, description: nameDialog.description.trim() || null })
        await qc.invalidateQueries({ queryKey: ['manual-maps'] })
        goToMap(data.id)
        toast.success('Map created — drag devices from the palette to start')
      } else if (nameDialog.kind === 'duplicate' && detail) {
        const newId = await cloneMap(detail, name, nameDialog.description.trim() || null)
        await qc.invalidateQueries({ queryKey: ['manual-maps'] })
        goToMap(newId)
        toast.success('Map duplicated')
      } else if (nameDialog.kind === 'rename' && selectedMapId) {
        await api.put(`/maps/${selectedMapId}`, { name, description: nameDialog.description.trim() || null })
        await qc.invalidateQueries({ queryKey: ['manual-maps'] })
        await qc.invalidateQueries({ queryKey: ['manual-map', selectedMapId] })
        toast.success('Map updated')
      }
      setNameDialog(null)
    } catch {
      toast.error(nameDialog.kind === 'new' ? 'Failed to create map' : nameDialog.kind === 'duplicate' ? 'Duplicate failed' : 'Failed to update map')
    } finally { setBusy(false) }
  }

  const deleteMap = async () => {
    if (!selectedMapId) return
    setBusy(true)
    try {
      await api.delete(`/maps/${selectedMapId}`)
      try { localStorage.removeItem(LAST_MANUAL_MAP_KEY) } catch { /* ignore */ }
      await qc.invalidateQueries({ queryKey: ['manual-maps'] })
      const remaining = maps.filter((m) => m.id !== selectedMapId)
      const next = new URLSearchParams(params)
      if (remaining.length) next.set('map', newestCreatedMapId(remaining)!); else next.delete('map')
      setParams(next, { replace: true })
      setDeleteOpen(false)
      toast.success('Map deleted')
    } catch { toast.error('Failed to delete map') } finally { setBusy(false) }
  }

  const openMapMenu = (el: HTMLElement) => {
    const r = el.getBoundingClientRect()
    const items: MenuItem[] = [{ type: 'header', label: 'Maps' }]
    for (const m of maps) {
      const sc = m.status_counts || {}
      const down = sc.down || 0
      items.push({
        type: 'item',
        label: `${m.id === selectedMapId ? '✓ ' : '   '}${m.name}  ·  ${m.node_count} dev${down ? `  ·  ${down} down` : ''}`,
        icon: down ? <AlertTriangle className="h-4 w-4 text-danger" /> : <MapIcon className="h-4 w-4" />,
        onClick: () => goToMap(m.id, mode),
      })
    }
    items.push({ type: 'divider' })
    items.push({ type: 'item', label: 'New map…', icon: <Plus className="h-4 w-4" />, onClick: () => setNameDialog({ kind: 'new', name: 'Untitled map', description: '' }) })
    setMapMenu({ x: r.left, y: r.bottom + 4, items })
  }

  /* Import a JSON backup as a NEW map. Device/server/check ids differ between
   * appliances, so an id that is not known here is re-matched by IP, then by
   * hostname; nodes that still match nothing are skipped and reported. */
  const importJson = async () => {
    const file = await pickTextFile()
    if (!file) return
    let backup: MapBackup
    try { backup = parseMapJson(file.text) } catch (e) { toast.error('Import failed', (e as Error).message); return }
    setBusy(true)
    try {
      const [servers, checks] = await Promise.all([
        qc.fetchQuery({ queryKey: ['servers', 'manual-map-picker'], queryFn: fetchMapServers }),
        qc.fetchQuery({ queryKey: ['service-checks', 'manual-map-picker'], queryFn: fetchMapServiceChecks }),
      ])
      const devs = devicesQ.data?.data || []
      const byKey = (list: { id: string; ip_address?: string; hostname?: string }[]) => ({
        ids: new Set(list.map((d) => d.id)),
        ip: new Map(list.filter((d) => d.ip_address).map((d) => [String(d.ip_address), d.id])),
        host: new Map(list.filter((d) => d.hostname).map((d) => [String(d.hostname).toLowerCase(), d.id])),
      })
      const idx = { device: byKey(devs), server: byKey(servers.data || []), service_check: byKey(checks.data || []) }
      let rematched = 0
      const nodes = backup.nodes.map((n) => {
        const k = idx[n.kind] || idx.device
        const field = n.kind === 'server' ? 'server_id' : n.kind === 'service_check' ? 'service_check_id' : 'device_id'
        const cur = (n as any)[field] as string | null
        if (cur && k.ids.has(cur)) return n
        const match = (n.ip_address && k.ip.get(String(n.ip_address))) || (n.hostname && k.host.get(String(n.hostname).toLowerCase()))
        if (!match) return n
        rematched++
        return { ...n, [field]: match }
      })
      const conv = toBulkPayload({ ...backup, nodes }, { devices: idx.device.ids, servers: idx.server.ids, service_checks: idx.service_check.ids })
      const { data: created } = await api.post('/maps', { name: `${backup.map.name} (imported)`, description: backup.map.description ?? null, metadata: conv.mapMetadata })
      try {
        await api.post(`/maps/${created.id}/bulk`, conv.payload)
      } catch (e) {
        // Don't leave an empty "(imported)" map behind.
        await api.delete(`/maps/${created.id}`).catch(() => {})
        throw e
      }
      await qc.invalidateQueries({ queryKey: ['manual-maps'] })
      goToMap(created.id)
      const notes = [rematched ? `${rematched} matched by IP/hostname` : '', conv.dropped.length ? `${conv.dropped.length} node${conv.dropped.length > 1 ? 's' : ''} skipped (not on this appliance)` : ''].filter(Boolean).join(' · ')
      toast.success('Map imported', notes || undefined)
    } catch (e: any) {
      toast.error('Import failed', e?.response?.data?.detail || e?.message || String(e))
    } finally { setBusy(false) }
  }

  const openActionsMenu = (el: HTMLElement) => {
    const r = el.getBoundingClientRect()
    const items: MenuItem[] = [
      { type: 'header', label: current?.name || 'Map' },
      { type: 'item', label: 'Rename / description…', icon: <Tag className="h-4 w-4" />, disabled: !detail, onClick: () => setNameDialog({ kind: 'rename', name: current?.name || '', description: current?.description || '' }) },
      { type: 'item', label: 'Duplicate as new map…', icon: <Copy className="h-4 w-4" />, disabled: !detail, onClick: () => setNameDialog({ kind: 'duplicate', name: `${current?.name || 'Map'} (copy)`, description: current?.description || '' }) },
      { type: 'item', label: 'Build from site / subnet…', icon: <Wand2 className="h-4 w-4" />, disabled: !detail, onClick: () => setBuildOpen(true) },
      { type: 'item', label: 'Verify topology', icon: <ShieldCheck className="h-4 w-4" />, disabled: !detail || mode !== 'design', onClick: () => canvasRef.current?.openVerify() },
      { type: 'item', label: 'Version history…', icon: <History className="h-4 w-4" />, disabled: !detail, onClick: () => setVersionsOpen(true) },
      { type: 'item', label: 'Open live view in new tab', icon: <Play className="h-4 w-4" />, disabled: !selectedMapId, onClick: () => window.open(`/maps/manual?map=${selectedMapId}&mode=live`, '_blank') },
      { type: 'divider' },
      ...exportMenuItems({
        detail,
        getNodes: () => canvasRef.current?.getNodes() || [],
        getViewportEl: () => canvasRef.current?.getViewportEl() || null,
        prepare: async () => (await canvasRef.current?.prepareExport()) || (() => {}),
      }),
      { type: 'item', label: 'Import JSON backup as new map…', icon: <FileUp className="h-4 w-4" />, onClick: () => void importJson() },
      { type: 'divider' },
      { type: 'item', label: 'Keyboard shortcuts', icon: <Keyboard className="h-4 w-4" />, onClick: () => setHelpOpen(true) },
      { type: 'divider' },
      { type: 'item', label: 'Delete map…', icon: <Trash2 className="h-4 w-4" />, danger: true, disabled: !detail, onClick: () => setDeleteOpen(true) },
    ]
    setMapMenu({ x: r.right - 240, y: Math.max(8, Math.min(r.bottom + 4, window.innerHeight - 40 - items.length * 30)), items })
  }

  const toggleFullscreen = useCallback(() => {
    const el = rootRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen().catch(() => toast.error('Fullscreen is not available in this browser'))
  }, [])

  // Placed entity ids (device, server or check) — the palette marks them "on map".
  const usedIds = useMemo(() => new Set((detail?.nodes || []).map((n) => n.device_id || n.server_id || n.service_check_id || '').filter(Boolean)), [detail])
  const noMaps = !mapsQuery.isLoading && maps.length === 0

  return (
    <div ref={rootRef} className="-m-5 flex h-[calc(100vh-2.75rem)] flex-col bg-bg" style={isFullscreen ? { margin: 0, height: '100vh' } : undefined}>
      {/* Top bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <Rows3 className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold text-text">Network Studio</span>
        </div>

        {/* Map switcher */}
        <button
          type="button"
          onClick={(e) => openMapMenu(e.currentTarget)}
          disabled={mapsQuery.isLoading}
          className="flex max-w-[20rem] items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-text transition hover:border-primary/50"
          title="Switch map"
        >
          <MapIcon className="h-3.5 w-3.5 text-muted" />
          <span className="truncate font-medium">{current?.name || (mapsQuery.isLoading ? 'Loading…' : 'No map')}</span>
          <ChevronDown className="h-3 w-3 text-muted" />
        </button>

        {detail && (
          <span className="text-xs text-muted" title={current?.description || undefined}>
            {counts.devices} devices{counts.servers ? ` · ${counts.servers} server${counts.servers > 1 ? 's' : ''}` : ''}{counts.checks ? ` · ${counts.checks} check${counts.checks > 1 ? 's' : ''}` : ''} · {counts.links} links{counts.shapes ? ` · ${counts.shapes} shapes` : ''}
          </span>
        )}

        <button
          type="button"
          onClick={() => setNameDialog({ kind: 'new', name: 'Untitled map', description: '' })}
          disabled={busy}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted transition hover:border-primary/50 hover:text-text disabled:opacity-50"
          title="Create a new blank map"
        >
          <Plus className="h-3.5 w-3.5" /> New
        </button>
        <button
          type="button"
          onClick={(e) => openActionsMenu(e.currentTarget)}
          disabled={busy || !selectedMapId}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted transition hover:border-primary/50 hover:text-text disabled:opacity-50"
          title="Map actions — rename, duplicate, delete"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Settings2 className="h-3.5 w-3.5" />} Map <MoreHorizontal className="h-3.5 w-3.5" />
        </button>

        <div className="flex-1" />

        {/* Live-only toggles */}
        {mode === 'live' && (
          <div className="flex items-center gap-1">
            <Toggle on={livePrefs.throughput} onClick={() => setLivePrefs((p) => ({ ...p, throughput: !p.throughput }))} title="Throughput pills on cables"><Activity className="h-3.5 w-3.5" /> Throughput</Toggle>
            <Toggle on={livePrefs.ifaceLabels} onClick={() => setLivePrefs((p) => ({ ...p, ifaceLabels: !p.ifaceLabels }))} title="Port labels at cable ends">Ports</Toggle>
            <Toggle on={livePrefs.animate} onClick={() => setLivePrefs((p) => ({ ...p, animate: !p.animate }))} title="Animated traffic particles (turn off on low-power wall PCs)">Flow</Toggle>
            <Toggle on={livePrefs.problems} onClick={() => setLivePrefs((p) => ({ ...p, problems: !p.problems }))} title="Attention list (down devices, faulted/hot/unbound links)">Attention</Toggle>
          </div>
        )}

        {/* Design / Live toggle */}
        <div className="flex items-center rounded-lg border border-border bg-surface p-0.5">
          <button
            type="button"
            onClick={() => switchMode('design')}
            className={cn('flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition', mode === 'design' ? 'bg-primary text-white' : 'text-muted hover:text-text')}
          >
            <Pencil className="h-3.5 w-3.5" /> Design
          </button>
          <button
            type="button"
            onClick={() => switchMode('live')}
            className={cn('flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition', mode === 'live' ? 'bg-success text-white' : 'text-muted hover:text-text')}
          >
            <Radio className="h-3.5 w-3.5" /> Live
          </button>
        </div>

        <button
          type="button"
          onClick={toggleFullscreen}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted transition hover:border-primary/50 hover:text-text"
          title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen (NOC wall)'}
        >
          {isFullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={() => setHelpOpen(true)}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted transition hover:border-primary/50 hover:text-text"
          title="Keyboard shortcuts"
        >
          <Keyboard className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Palette + canvas */}
      <div className="flex min-h-0 flex-1">
        {mode === 'design' && !noMaps && (
          <DevicePalette
            open={paletteOpen}
            onToggle={() => setPaletteOpen((v) => !v)}
            usedIds={usedIds}
            disabled={!selectedMapId}
            onInsert={(spec) => insertRef.current?.(spec)}
          />
        )}
        <div className="relative min-h-0 flex-1">
          {mapsQuery.isLoading || (selectedMapId && mapQuery.isLoading) ? (
            <div className="flex h-full items-center justify-center text-muted">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading map…
            </div>
          ) : noMaps ? (
            <EmptyState onCreate={() => setNameDialog({ kind: 'new', name: 'Untitled map', description: '' })} />
          ) : detail ? (
            <ReactFlowProvider>
              <MapCanvas
                mapId={selectedMapId!}
                detail={detail}
                liveData={liveData}
                nodesLive={nodesLive}
                liveUpdatedAt={Math.max(liveLinksQuery.dataUpdatedAt, nodesLiveQuery.dataUpdatedAt)}
                liveMode={mode === 'live'}
                showThroughput={livePrefs.throughput}
                // The Ports toggle only exists in Live; Design always shows port
                // labels so a toggle set on the wall never hides them while editing.
                showIfaceLabels={mode === 'live' ? livePrefs.ifaceLabels : true}
                animate={livePrefs.animate}
                showProblems={livePrefs.problems}
                insertRef={insertRef}
                focusDeviceId={params.get('focus')}
                actionsRef={canvasRef}
              />
            </ReactFlowProvider>
          ) : mapQuery.isError ? (
            <div className="flex h-full items-center justify-center text-danger">Failed to load this map.</div>
          ) : null}
        </div>
      </div>

      <ContextMenu state={mapMenu} onClose={() => setMapMenu(null)} />

      {nameDialog && (
        <Dialog open onOpenChange={(o) => { if (!o) setNameDialog(null) }}>
          <DialogContent className="max-w-sm">
            <DialogHeader>
              <DialogTitle>{nameDialog.kind === 'new' ? 'New map' : nameDialog.kind === 'duplicate' ? 'Duplicate map' : 'Map details'}</DialogTitle>
            </DialogHeader>
            {nameDialog.kind === 'duplicate' && <DialogDescription>Copies every device, shape and cable into a new map you can edit independently.</DialogDescription>}
            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium text-muted">Name</span>
              <input
                autoFocus
                value={nameDialog.name}
                onChange={(e) => setNameDialog({ ...nameDialog, name: e.target.value })}
                onKeyDown={(e) => { if (e.key === 'Enter') void submitName() }}
                className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium text-muted">Description (optional)</span>
              <input
                value={nameDialog.description}
                onChange={(e) => setNameDialog({ ...nameDialog, description: e.target.value })}
                onKeyDown={(e) => { if (e.key === 'Enter') void submitName() }}
                placeholder="e.g. HQ campus core & distribution"
                className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60"
              />
            </label>
            <DialogFooter>
              <Button variant="outline" onClick={() => setNameDialog(null)} disabled={busy}>Cancel</Button>
              <Button onClick={() => void submitName()} disabled={busy}>{busy ? 'Working…' : nameDialog.kind === 'new' ? 'Create' : nameDialog.kind === 'duplicate' ? 'Duplicate' : 'Save'}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete this map?"
        description={`"${current?.name || 'This map'}" and all its placed devices, cables and shapes will be removed. Monitored devices are not affected.`}
        confirmText="Delete map"
        destructive
        loading={busy}
        onConfirm={() => void deleteMap()}
      />

      {helpOpen && <ShortcutsDialog onClose={() => setHelpOpen(false)} />}

      {versionsOpen && selectedMapId && (
        <VersionsDialog mapId={selectedMapId} onClose={() => setVersionsOpen(false)} onRestored={() => { setVersionsOpen(false); canvasRef.current?.resetHistory(); void qc.invalidateQueries({ queryKey: ['manual-maps'] }) }} />
      )}

      {buildOpen && selectedMapId && (
        <BuildMapDialog
          mapId={selectedMapId}
          devices={(devicesQ.data?.data || []).map((d) => ({ id: d.id, hostname: d.hostname, ip_address: d.ip_address }))}
          onClose={() => setBuildOpen(false)}
          onBuilt={() => { setBuildOpen(false); canvasRef.current?.resetHistory(); void qc.invalidateQueries({ queryKey: ['manual-maps'] }) }}
        />
      )}
    </div>
  )
}

function Toggle({ on, onClick, title, children }: { on: boolean; onClick: () => void; title: string; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn('flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium transition', on ? 'border-success/40 bg-success/10 text-success' : 'border-border text-muted hover:text-text')}
      title={title}
    >
      {on && <Check className="h-3 w-3" />}{children}
    </button>
  )
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-border bg-surface text-primary shadow"><MapIcon className="h-7 w-7" /></div>
      <div>
        <div className="text-sm font-semibold text-text">No maps yet</div>
        <div className="mt-1 max-w-xs text-xs text-muted">Create a map, then drag devices from the palette, draw cables between them and switch to Live to watch traffic.</div>
      </div>
      <Button onClick={onCreate}><Plus className="mr-1 h-4 w-4" /> Create your first map</Button>
    </div>
  )
}

const SHORTCUTS: [string, string][] = [
  ['V', 'Select tool · drag on empty canvas box-selects'],
  ['C', 'Connect tool (or drag the blue plug on a hovered device)'],
  ['Space + drag', 'Pan the canvas (middle mouse also pans)'],
  ['Wheel', 'Zoom at cursor · + / − zoom · F or 0 fit to screen'],
  ['Shift / Ctrl + click', 'Add to selection'],
  ['Ctrl + A', 'Select all'],
  ['Arrows / Shift + Arrows', 'Nudge selection 10 px / 40 px'],
  ['Ctrl + C / Ctrl + V', 'Copy / paste selection (cables between copied items included)'],
  ['Ctrl + D', 'Duplicate selection'],
  ['Ctrl + G / Ctrl + Shift + G', 'Group / ungroup'],
  ['L', 'Lock / unlock selection'],
  ['Delete / Backspace', 'Remove selection (asks to confirm)'],
  ['Ctrl + Z / Ctrl + Shift + Z', 'Undo / redo'],
  ['Double-click device', 'Edit label, icon, size'],
  ['Double-click cable', 'Edit ports, type, colour, arrows'],
  ['Double-click shape', 'Edit its text / caption'],
  ['Right-click', 'Context menu (canvas · device · cable · shape)'],
  ['Esc', 'Clear selection / close menus'],
]

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Keyboard & mouse</DialogTitle></DialogHeader>
        <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
          {SHORTCUTS.map(([k, v]) => (
            <div key={k} className="contents">
              <kbd className="whitespace-nowrap rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-[10px] text-text">{k}</kbd>
              <span className="text-text2">{v}</span>
            </div>
          ))}
        </div>
        <DialogFooter><Button variant="outline" onClick={onClose}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* Deep-copy a map into a brand-new one: one bulk request creates every node
 * (devices, servers, checks), shape and cable, using the source ids as client
 * ids so cables and annotation cables are remapped server-side. */
async function cloneMap(detail: ManualMapDetail, name: string, description: string | null): Promise<string> {
  const srcMeta = (detail.metadata as any) || {}
  const { annotation_links: annLinks = [], ...uiMeta } = srcMeta
  const { data: created } = await api.post('/maps', { name, description, metadata: uiMeta })
  const newId: string = created.id
  try {
    await cloneContent(detail, newId, annLinks as any[])
  } catch (e) {
    // Don't leave an empty "(copy)" map behind.
    await api.delete(`/maps/${newId}`).catch(() => {})
    throw e
  }
  return newId
}

async function cloneContent(detail: ManualMapDetail, newId: string, annLinks: any[]) {
  const nodeIds = new Set(detail.nodes.map((n) => n.id))
  const shapeIds = new Set((detail.shapes || []).map((sh) => sh.id))
  await api.post(`/maps/${newId}/bulk`, {
    nodes: detail.nodes.map((n) => ({
      client_id: n.id, device_id: n.device_id, server_id: n.server_id ?? null, service_check_id: n.service_check_id ?? null,
      label: n.label && n.label !== n.hostname ? n.label : null, icon: n.icon || 'auto', x_pct: n.x_pct, y_pct: n.y_pct, metadata: n.metadata || {},
    })),
    shapes: (detail.shapes || []).map((sh) => ({
      client_id: sh.id, kind: sh.kind, x_pct: sh.x_pct, y_pct: sh.y_pct, w_pct: sh.w_pct, h_pct: sh.h_pct,
      text: sh.text, fill: sh.fill, stroke: sh.stroke, z_index: sh.z_index, metadata: sh.metadata || {},
    })),
    links: detail.links.map((l) => ({ source: l.source_node_id, target: l.target_node_id, label: l.label, link_type: l.link_type, metadata: l.metadata || {} })),
    // Drop cables whose endpoint no longer exists on the source map.
    annotation_links: (annLinks as any[])
      .filter((al) => (nodeIds.has(al.source) || shapeIds.has(al.source)) && (nodeIds.has(al.target) || shapeIds.has(al.target)))
      .map((al) => ({ ...al, id: undefined })),
  })
}

export type { ManualMapListItem }
