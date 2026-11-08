import { useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { ArrowLeft, ChevronRight, Loader2, Pause, Play, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { applyDensity, useDisplay, type Density } from '@/stores/display'
import { MapCanvas } from './canvas/MapCanvas'
import { NocStatusBar } from './canvas/NocOverlays'
import { useLiveLinks, useManualMap, useNodesLive, useSitesLive } from './useMapData'
import type { ManualMapListItem } from './core'

const EMPTY: Record<string, never> = {}

/** Wall text size from the screen: desk monitors stay 1×, 4K walls grow. */
function wallBoost(width: number): number {
  if (width >= 3400) return 2.1
  if (width >= 2500) return 1.6
  if (width >= 1900) return 1.25
  return 1
}

function wallDensity(width: number): Density {
  if (width >= 2500) return 'wall'
  if (width >= 1700) return 'large'
  return 'comfortable'
}

/* NOC wall: one map (or a rotation of maps) on a dark, high-contrast screen
 * with no application chrome. Live data refreshes on its own (links and
 * nodes every 15 s, the map every 30 s), the map re-fits whenever the screen
 * size changes, and text grows with the display so a 4K video wall reads
 * from across the room while a desktop monitor still looks right.
 *
 * URL: /maps/manual?wall=1&map=<id>[&maps=<id>,<id>…&rotate=<seconds>]
 *      [&attention=0] [&scale=<text multiplier>] [&kiosk=1]
 * Esc or the Exit button (shown when the mouse moves) leaves the wall. */
export function MapWall({ maps, startId, rotateIds, rotateSec, onExit, showAttention, scaleOverride }: {
  maps: ManualMapListItem[]
  startId: string
  rotateIds: string[]
  rotateSec: number
  onExit: () => void
  showAttention: boolean
  scaleOverride: number | null
}) {
  const playlist = rotateIds.length ? rotateIds : [startId]
  const [index, setIndex] = useState(() => Math.max(0, playlist.indexOf(startId)))
  const [paused, setPaused] = useState(false)
  // Drill-down stack: sites opened from the wall. Rotation pauses while drilled in.
  const [trail, setTrail] = useState<string[]>([])
  const baseId = playlist[index % playlist.length] || startId
  const mapId = trail.length ? trail[trail.length - 1] : baseId
  const rotating = playlist.length > 1 && !paused && trail.length === 0

  const [width, setWidth] = useState(() => window.innerWidth)
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Scale the whole UI with the screen while the wall is open; restore after.
  useEffect(() => {
    const before = useDisplay.getState().density
    const urlDensity = new URLSearchParams(window.location.search).get('density')
    if (!urlDensity) applyDensity(wallDensity(width))
    return () => applyDensity(before)
  }, [width])

  useEffect(() => {
    if (!rotating) return
    const t = setTimeout(() => setIndex((i) => (i + 1) % playlist.length), rotateSec * 1000)
    return () => clearTimeout(t)
  }, [rotating, index, playlist.length, rotateSec])

  // Hide the cursor and the exit button when the mouse rests.
  const [idle, setIdle] = useState(false)
  const idleTimer = useRef<number | null>(null)
  useEffect(() => {
    const wake = () => {
      setIdle(false)
      if (idleTimer.current) window.clearTimeout(idleTimer.current)
      idleTimer.current = window.setTimeout(() => setIdle(true), 3500)
    }
    wake()
    window.addEventListener('mousemove', wake)
    window.addEventListener('keydown', wake)
    return () => {
      window.removeEventListener('mousemove', wake)
      window.removeEventListener('keydown', wake)
      if (idleTimer.current) window.clearTimeout(idleTimer.current)
    }
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('[role="dialog"]')) {
        if (trail.length) setTrail((t) => t.slice(0, -1))
        else onExit()
      }
      if (e.key === ' ' && playlist.length > 1) { e.preventDefault(); setPaused((p) => !p) }
      if (e.key === 'ArrowRight' && playlist.length > 1) setIndex((i) => (i + 1) % playlist.length)
      if (e.key === 'ArrowLeft' && playlist.length > 1) setIndex((i) => (i - 1 + playlist.length) % playlist.length)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onExit, trail.length, playlist.length])

  const mapQ = useManualMap(mapId, true)
  const detail = mapQ.data
  const linksQ = useLiveLinks(mapId, true)
  const nodesQ = useNodesLive(mapId, true)
  const hasSites = !!detail?.shapes?.some((sh) => sh.metadata?.widget === 'site')
  const sitesQ = useSitesLive(mapId, true, hasSites)
  const liveData = linksQ.data?.data || EMPTY
  const nodesLive = nodesQ.data?.data || EMPTY
  const sitesLive = sitesQ.data?.data || EMPTY
  const updatedAt = Math.max(linksQ.dataUpdatedAt, nodesQ.dataUpdatedAt)
  const nameOf = (id: string) => maps.find((m) => m.id === id)?.name || (id === mapId ? detail?.name : undefined) || 'Map'
  const boost = scaleOverride ?? wallBoost(width)
  const crumbs = useMemo(() => [baseId, ...trail], [baseId, trail])

  return (
    <div className={cn('dark map-wall fixed inset-0 z-[80] flex flex-col text-text', idle && 'cursor-idle')}>
      {/* Header: map name / drill trail, summary tiles */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 pb-2 pt-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-[0.2em] text-muted">
            Network map{playlist.length > 1 && trail.length === 0 ? ` · ${(index % playlist.length) + 1} of ${playlist.length}` : ''}
          </div>
          <div className="flex min-w-0 items-center gap-1.5">
            {trail.length > 0 && (
              <button type="button" onClick={() => setTrail((t) => t.slice(0, -1))} className="rounded-md p-1 text-muted hover:bg-surface2 hover:text-text" title="Back (Esc)">
                <ArrowLeft className="h-5 w-5" />
              </button>
            )}
            {crumbs.map((id, i) => (
              <span key={`${id}-${i}`} className="flex min-w-0 items-center gap-1.5">
                {i > 0 && <ChevronRight className="h-5 w-5 shrink-0 text-muted" />}
                <button
                  type="button"
                  disabled={i === crumbs.length - 1}
                  onClick={() => setTrail(crumbs.slice(1, i + 1))}
                  className={cn('truncate font-bold', i === crumbs.length - 1 ? 'text-2xl text-text' : 'text-xl text-muted hover:text-text')}
                >
                  {nameOf(id)}
                </button>
              </span>
            ))}
          </div>
        </div>
        <div className="ml-auto">
          {detail && (
            <NocStatusBar detail={detail} nodesLive={nodesLive} liveData={liveData} sitesLive={sitesLive} updatedAt={updatedAt || Date.now()} size="wall" />
          )}
        </div>
      </div>

      {/* Canvas */}
      <div className="relative min-h-0 flex-1">
        {detail ? (
          <ReactFlowProvider key={mapId}>
            <MapCanvas
              mapId={mapId}
              detail={detail}
              liveData={liveData}
              nodesLive={nodesLive}
              sitesLive={sitesLive}
              liveUpdatedAt={updatedAt}
              liveMode
              wall
              textBoost={boost}
              showThroughput
              showIfaceLabels={false}
              animate
              showProblems={showAttention}
              maps={maps}
              onOpenMap={(id) => setTrail((t) => [...t, id])}
            />
          </ReactFlowProvider>
        ) : mapQ.isError ? (
          <div className="flex h-full items-center justify-center text-xl text-danger">This map could not be loaded.</div>
        ) : (
          <div className="flex h-full items-center justify-center text-muted"><Loader2 className="mr-2 h-6 w-6 animate-spin" /> Loading map…</div>
        )}

        {/* Controls appear with the mouse */}
        <div className={cn('absolute bottom-4 right-4 z-10 flex items-center gap-2 transition-opacity duration-300', idle ? 'pointer-events-none opacity-0' : 'opacity-100')}>
          {playlist.length > 1 && (
            <button type="button" onClick={() => setPaused((p) => !p)} className="flex items-center gap-1.5 rounded-lg border border-border bg-surface/90 px-3 py-1.5 text-sm font-medium text-text2 shadow-lg hover:text-text" title="Pause or resume the rotation (Space)">
              {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />} {paused ? 'Resume' : 'Pause'}
            </button>
          )}
          <button type="button" onClick={onExit} className="flex items-center gap-1.5 rounded-lg border border-border bg-surface/90 px-3 py-1.5 text-sm font-medium text-text2 shadow-lg hover:text-text" title="Leave the wall view (Esc)">
            <X className="h-4 w-4" /> Exit wall
          </button>
        </div>
      </div>

      {/* Rotation progress */}
      {playlist.length > 1 && (
        <div className="h-1 w-full bg-surface2">
          {rotating && <div key={`${index}-${paused}`} className="nm-wall-progress h-full bg-primary/70" style={{ animationDuration: `${rotateSec}s` }} />}
        </div>
      )}
    </div>
  )
}
