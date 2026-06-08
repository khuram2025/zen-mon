import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ReactFlowProvider } from '@xyflow/react'
import { Activity, Loader2, Pencil, Radio, Rows3 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage } from '@/lib/utils'
import { MapCanvas } from './canvas/MapCanvas'
import { useLiveLinks, useManualMap, useManualMaps, useNodeMutations } from './useMapData'

type Mode = 'design' | 'live'

export function MapEditorV2() {
  const [params, setParams] = useSearchParams()
  const [mode, setMode] = useState<Mode>('design')
  const [showThroughput, setShowThroughput] = useState(true)

  const mapsQuery = useManualMaps()
  const maps = mapsQuery.data?.data || []

  const urlMapId = params.get('map')
  const selectedMapId = urlMapId || maps[0]?.id || null

  // Keep ?map in the URL once we know the default, so deep links work.
  useEffect(() => {
    if (!urlMapId && maps[0]?.id) {
      const next = new URLSearchParams(params)
      next.set('map', maps[0].id)
      next.set('v2', '1')
      setParams(next, { replace: true })
    }
  }, [urlMapId, maps, params, setParams])

  const mapQuery = useManualMap(selectedMapId)
  const detail = mapQuery.data
  const liveLinksQuery = useLiveLinks(selectedMapId, mode === 'live')
  const liveData = liveLinksQuery.data?.data || {}

  const { move } = useNodeMutations(selectedMapId)

  const counts = useMemo(() => ({ nodes: detail?.nodes.length || 0, links: detail?.links.length || 0 }), [detail])

  function persistPosition(id: string, x_pct: number, y_pct: number) {
    move.mutate({ id, x_pct, y_pct }, { onError: (e) => toast.error(apiErrorMessage(e)) })
  }

  return (
    <div className="-m-5 flex h-[calc(100vh-2.75rem)] flex-col bg-bg">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-border bg-surface/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <Rows3 className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold text-text">Network Studio</span>
          <span className="rounded bg-primary/15 px-1.5 py-px text-[10px] font-bold uppercase tracking-wide text-primary">v2</span>
        </div>

        {/* Map switcher */}
        <select
          className="ml-2 max-w-[16rem] rounded-md border border-border bg-surface px-2 py-1 text-xs text-text"
          value={selectedMapId || ''}
          onChange={(e) => {
            const next = new URLSearchParams(params)
            next.set('map', e.target.value)
            next.set('v2', '1')
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

        <div className="flex-1" />

        {/* Design / Live toggle */}
        <div className="flex items-center rounded-lg border border-border bg-surface p-0.5">
          <button
            type="button"
            onClick={() => setMode('design')}
            className={cn('flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition', mode === 'design' ? 'bg-primary text-white' : 'text-muted hover:text-text')}
          >
            <Pencil className="h-3.5 w-3.5" /> Design
          </button>
          <button
            type="button"
            onClick={() => setMode('live')}
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

        <Link to="/maps/manual" className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:text-text" title="Switch to the classic editor">
          Classic
        </Link>
      </div>

      {/* Canvas */}
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
              detail={detail}
              liveData={liveData}
              liveMode={mode === 'live'}
              showThroughput={showThroughput}
              onPersistPosition={persistPosition}
            />
          </ReactFlowProvider>
        ) : null}
      </div>
    </div>
  )
}
