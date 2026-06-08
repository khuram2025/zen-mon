import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  ConnectionMode,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  AlignHorizontalJustifyCenter,
  AlignStartHorizontal,
  AlignStartVertical,
  Cable,
  Grid3x3,
  Magnet,
  MousePointer2,
  Spline,
  Trash2,
  Wand2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  discCenterToNodeXY,
  nodeXYToDiscCenter,
  pctToPx,
  pxToPct,
  statusKey,
  type LiveLinkData,
  type ManualMapDetail,
  type ManualMapNode,
} from '../core'
import { useMapMutations } from '../useMapData'
import { DeviceNode } from './DeviceNode'
import { NetworkEdge } from './NetworkEdge'
import { ContextMenu, type ContextMenuState, type MenuItem } from './ContextMenu'
import { computeAlign, snap, type AlignOp } from './align'
import { MapModeContext } from './MapModeContext'
import { LinkDialog, type NewLink } from './LinkDialog'

const nodeTypes = { device: DeviceNode }
const edgeTypes = { network: NetworkEdge }
const GRID = 40 // logical px

const STATUS_HEX: Record<string, string> = {
  up: '#22c55e', down: '#ef4444', degraded: '#f59e0b', maintenance: '#3b82f6', unknown: '#6b7280',
}

export type MapCanvasProps = {
  mapId: string
  detail: ManualMapDetail
  liveData: Record<string, LiveLinkData>
  liveMode: boolean
  showThroughput: boolean
}

export function MapCanvas({ mapId, detail, liveData, liveMode, showThroughput }: MapCanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [snapOn, setSnapOn] = useState(false)
  const [gridOn, setGridOn] = useState(true)
  const [menu, setMenu] = useState<ContextMenuState>(null)
  const [selCount, setSelCount] = useState(0)
  const [tool, setTool] = useState<'select' | 'connect'>('select')
  // All deletions are explicit + confirmed. Nothing deletes without this.
  const [pendingDelete, setPendingDelete] = useState<{ title: string; description: string; run: () => void } | null>(null)
  const [pendingLink, setPendingLink] = useState<{ source: ManualMapNode; target: ManualMapNode } | null>(null)

  const connectMode = tool === 'connect' && !liveMode
  const { bulkMove, deleteNode, deleteLink, updateLink, updateNode, addLink } = useMapMutations(mapId)
  const nodeById = useMemo(() => new Map(detail.nodes.map((n) => [n.id, n])), [detail.nodes])

  // Mirrors so callbacks can read the latest local metadata.
  const edgesRef = useRef<Edge[]>([])
  useEffect(() => { edgesRef.current = edges }, [edges])
  const nodesRef = useRef<Node[]>([])
  useEffect(() => { nodesRef.current = nodes }, [nodes])

  /* ── Node metadata (movable label offset) — ref-stable like the edge ones ── */
  const patchNodeMetaRef = useRef<(nodeId: string, patch: Record<string, unknown>, commit: boolean) => void>(() => {})
  patchNodeMetaRef.current = (nodeId, patch, commit) => {
    setNodes((nds) => nds.map((n) => {
      if (n.id !== nodeId) return n
      const nd = (n.data as any).node
      return { ...n, data: { ...n.data, node: { ...nd, metadata: { ...(nd.metadata || {}), ...patch } } } }
    }))
    if (commit) {
      const cur = (nodesRef.current.find((n) => n.id === nodeId)?.data as any)?.node
      updateNode.mutate({ id: nodeId, patch: { metadata: { ...(cur?.metadata || {}), ...patch } } }, { onError: () => toast.error('Failed to save label') })
    }
  }
  const setNodeLabelOffset = useCallback((nodeId: string, dx: number, dy: number, commit: boolean) => {
    patchNodeMetaRef.current(nodeId, { label_offset: { dx, dy } }, commit)
  }, [])

  /* ── Link shape / waypoint editing ───────────────────────────────
   * These are kept REF-stable so they can live in the edge-data closure
   * without making the edge-rebuild effect refire each render (which would
   * wipe selection + uncommitted bends). */
  const patchLinkMetaRef = useRef<(linkId: string, patch: Record<string, unknown>, commit: boolean) => void>(() => {})
  patchLinkMetaRef.current = (linkId, patch, commit) => {
    setEdges((eds) => eds.map((e) => {
      if (e.id !== linkId) return e
      const link = (e.data as any).link
      return { ...e, data: { ...e.data, link: { ...link, metadata: { ...(link.metadata || {}), ...patch } } } }
    }))
    if (commit) {
      const cur = (edgesRef.current.find((e) => e.id === linkId)?.data as any)?.link
      const meta = { ...(cur?.metadata || {}), ...patch }
      updateLink.mutate({ id: linkId, patch: { metadata: meta } }, { onError: () => toast.error('Failed to save link') })
    }
  }

  const setEdgeWaypoints = useCallback((linkId: string, wpsPx: { x: number; y: number }[], commit: boolean) => {
    patchLinkMetaRef.current(linkId, { waypoints: wpsPx.map((p) => pxToPct(p.x, p.y)) }, commit)
  }, [])

  const setEdgeShape = useCallback((linkId: string, shape: 'curve' | 'straight' | 'orthogonal') => {
    patchLinkMetaRef.current(linkId, { shape }, true)
  }, [])

  // Preserve selection across server-driven rebuilds.
  const selectedIds = useRef<Set<string>>(new Set())

  useEffect(() => {
    setNodes(
      detail.nodes.map((n) => {
        const c = pctToPx(n)
        return {
          id: n.id,
          type: 'device',
          position: discCenterToNodeXY(c.x, c.y),
          data: {
            node: n,
            live: liveMode,
            onLabelMove: (dx: number, dy: number, commit: boolean) => setNodeLabelOffset(n.id, dx, dy, commit),
          },
          draggable: !liveMode,
          selected: selectedIds.current.has(n.id),
        } satisfies Node
      }),
    )
  }, [detail.nodes, liveMode, setNodes, setNodeLabelOffset])

  useEffect(() => {
    setEdges(
      detail.links
        .filter((l) => nodeById.has(l.source_node_id) && nodeById.has(l.target_node_id))
        .map((l) => {
          const s = nodeById.get(l.source_node_id)!
          const t = nodeById.get(l.target_node_id)!
          return {
            id: l.id,
            source: l.source_node_id,
            target: l.target_node_id,
            sourceHandle: 'c',
            targetHandle: 'c',
            type: 'network',
            data: {
              link: l,
              sourceStatus: s.status,
              targetStatus: t.status,
              live: liveData[l.id],
              liveMode,
              showThroughput,
              setWaypoints: (wpsPx: { x: number; y: number }[], commit: boolean) => setEdgeWaypoints(l.id, wpsPx, commit),
            },
          } satisfies Edge
        }),
    )
  }, [detail.links, liveData, liveMode, showThroughput, nodeById, setEdges, setEdgeWaypoints])

  /* ── Persistence ─────────────────────────────────────────────── */
  const persistPositions = useCallback((items: { id: string; x: number; y: number }[]) => {
    const payload = items.map((it) => {
      const c = nodeXYToDiscCenter(it.x, it.y)
      const p = pxToPct(c.x, c.y)
      return { id: it.id, x_pct: p.x_pct, y_pct: p.y_pct }
    })
    bulkMove.mutate(payload, { onError: () => toast.error('Failed to save layout') })
  }, [bulkMove])

  // Drag-to-connect → open the new-link dialog.
  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return
    const s = nodeById.get(c.source)
    const t = nodeById.get(c.target)
    if (s && t) setPendingLink({ source: s, target: t })
  }, [nodeById])

  const createLink = useCallback((link: NewLink) => {
    addLink.mutate(link, {
      onSuccess: () => { setPendingLink(null); toast.success('Link created') },
      onError: (e: any) => {
        // Keep the dialog open so the user can adjust; surface a clear message.
        toast.error(e?.response?.status === 409 ? 'A link between these two devices already exists.' : 'Failed to create link')
      },
    })
  }, [addLink])

  const onNodeDragStop = useCallback((_e: unknown, node: Node, dragged: Node[]) => {
    const items = (dragged && dragged.length ? dragged : [node]).map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    persistPositions(items)
  }, [persistPositions])

  // NOTE: we intentionally do NOT wire RF's onNodesDelete/onEdgesDelete to the
  // backend, and deleteKeyCode is disabled — every deletion goes through an
  // explicit ConfirmDialog so nothing can ever be removed implicitly.

  /* ── Alignment / auto-align ──────────────────────────────────── */
  const applyAlign = useCallback((op: AlignOp) => {
    const sel = nodes.filter((n) => n.selected)
    if (sel.length < 2) return
    const moves = computeAlign(sel, op)
    setNodes((nds) => nds.map((n) => (moves.has(n.id) ? { ...n, position: moves.get(n.id)! } : n)))
    persistPositions(sel.map((n) => ({ id: n.id, ...(moves.get(n.id) || n.position) })))
  }, [nodes, setNodes, persistPositions])

  const autoAlign = useCallback(() => {
    const all = nodes
    if (!all.length) return
    setNodes((nds) => nds.map((n) => ({ ...n, position: { x: snap(n.position.x, GRID), y: snap(n.position.y, GRID) } })))
    persistPositions(all.map((n) => ({ id: n.id, x: snap(n.position.x, GRID), y: snap(n.position.y, GRID) })))
    toast.success('Aligned all to grid')
  }, [nodes, setNodes, persistPositions])

  /* ── Selection tracking + keyboard ───────────────────────────── */
  const onSelectionChange = useCallback(({ nodes: sel }: { nodes: Node[] }) => {
    selectedIds.current = new Set(sel.map((n) => n.id))
    setSelCount(sel.length)
  }, [])

  const wrapperRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = wrapperRef.current
    if (!el) return
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
        e.preventDefault()
        setNodes((nds) => nds.map((n) => ({ ...n, selected: true })))
      } else if (!e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'v') {
        setTool('select')
      } else if (!e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'c') {
        setTool('connect')
      }
    }
    el.addEventListener('keydown', onKey)
    return () => el.removeEventListener('keydown', onKey)
  }, [setNodes])

  /* ── Context menus ───────────────────────────────────────────── */
  const closeMenu = useCallback(() => setMenu(null), [])

  const onPaneContextMenu = useCallback((e: MouseEvent | React.MouseEvent) => {
    e.preventDefault()
    const items: MenuItem[] = [
      { type: 'header', label: 'Canvas' },
      { type: 'item', label: 'Select all', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => setNodes((nds) => nds.map((n) => ({ ...n, selected: true }))) },
      { type: 'item', label: 'Auto-align to grid', icon: <Wand2 className="h-4 w-4" />, onClick: autoAlign },
    ]
    setMenu({ x: (e as MouseEvent).clientX, y: (e as MouseEvent).clientY, items })
  }, [setNodes, autoAlign])

  const onNodeContextMenu = useCallback((e: React.MouseEvent, node: Node) => {
    e.preventDefault()
    // Ensure the node is part of the selection.
    if (!node.selected) setNodes((nds) => nds.map((n) => ({ ...n, selected: n.id === node.id })))
    const selIds = node.selected ? selectedIds.current : new Set([node.id])
    const multi = selIds.size > 1
    const dev = (node.data as any)?.node
    const items: MenuItem[] = []
    if (multi) {
      items.push({ type: 'header', label: `${selIds.size} selected` })
      items.push({ type: 'item', label: 'Align left', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => applyAlign('left') })
      items.push({ type: 'item', label: 'Align top', icon: <AlignStartHorizontal className="h-4 w-4" />, onClick: () => applyAlign('top') })
      items.push({ type: 'item', label: 'Center horizontally', icon: <AlignHorizontalJustifyCenter className="h-4 w-4" />, onClick: () => applyAlign('center-h') })
      items.push({ type: 'item', label: 'Distribute horizontally', onClick: () => applyAlign('distribute-h') })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Delete selected', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => deleteSelected() })
    } else {
      items.push({ type: 'header', label: dev?.hostname || 'Node' })
      if (dev?.device_id) items.push({ type: 'item', label: 'Open device', onClick: () => window.open(`/devices/${dev.device_id}`, '_blank') })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Remove from map', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: 'Remove node?',
        description: `Remove "${dev?.hostname || 'this node'}" from this map? The device itself is not affected.`,
        run: () => { deleteNode.mutate(node.id); setNodes((nds) => nds.filter((n) => n.id !== node.id)) },
      }) })
    }
    setMenu({ x: e.clientX, y: e.clientY, items })
  }, [setNodes, applyAlign, deleteNode])

  const onEdgeContextMenu = useCallback((e: React.MouseEvent, edge: Edge) => {
    e.preventDefault()
    const link = (edge.data as any).link
    const shape = link?.metadata?.shape || 'curve'
    const shapeItem = (label: string, val: 'curve' | 'straight' | 'orthogonal'): MenuItem =>
      ({ type: 'item', label: `${shape === val ? '✓ ' : '   '}${label}`, onClick: () => setEdgeShape(edge.id, val) })
    const items: MenuItem[] = [
      { type: 'header', label: 'Link shape' },
      shapeItem('Curved', 'curve'),
      shapeItem('Straight', 'straight'),
      shapeItem('Orthogonal', 'orthogonal'),
      { type: 'divider' },
      { type: 'item', label: 'Reset bends', icon: <Spline className="h-4 w-4" />, onClick: () => setEdgeWaypoints(edge.id, [], true) },
      { type: 'divider' },
      { type: 'item', label: 'Delete link', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: 'Delete link?',
        description: 'This removes the connection between the two nodes.',
        run: () => { deleteLink.mutate(edge.id); setEdges((eds) => eds.filter((x) => x.id !== edge.id)) },
      }) },
    ]
    setMenu({ x: e.clientX, y: e.clientY, items })
  }, [setEdgeShape, setEdgeWaypoints, deleteLink, setEdges])

  // Explicit edge selection (RF's built-in click-select is unreliable for our
  // fully-custom edge); selecting an edge reveals its bend handles.
  const onEdgeClick = useCallback((_e: React.MouseEvent, edge: Edge) => {
    setEdges((eds) => eds.map((x) => (x.selected === (x.id === edge.id) ? x : { ...x, selected: x.id === edge.id })))
    setNodes((nds) => nds.some((n) => n.selected) ? nds.map((n) => (n.selected ? { ...n, selected: false } : n)) : nds)
  }, [setEdges, setNodes])

  const deleteSelected = useCallback(() => {
    const sel = nodes.filter((n) => n.selected)
    if (!sel.length) return
    setPendingDelete({
      title: `Remove ${sel.length} node${sel.length > 1 ? 's' : ''}?`,
      description: `This removes ${sel.length === 1 ? 'it' : 'them'} from this map (the device itself is not affected).`,
      run: () => {
        sel.forEach((n) => { deleteNode.mutate(n.id); selectedIds.current.delete(n.id) })
        setNodes((nds) => nds.filter((n) => !n.selected))
        toast.success(`Removed ${sel.length} node${sel.length > 1 ? 's' : ''}`)
      },
    })
  }, [nodes, deleteNode, setNodes])

  return (
    <MapModeContext.Provider value={{ connectMode }}>
    <div ref={wrapperRef} tabIndex={0} className={cn('h-full w-full outline-none', connectMode && 'cursor-crosshair')}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        connectionMode={ConnectionMode.Loose}
        onSelectionChange={onSelectionChange}
        onPaneContextMenu={onPaneContextMenu}
        onNodeContextMenu={onNodeContextMenu}
        onEdgeContextMenu={onEdgeContextMenu}
        onEdgeClick={onEdgeClick}
        onPaneClick={() => { closeMenu(); setEdges((eds) => eds.some((e) => e.selected) ? eds.map((e) => ({ ...e, selected: false })) : eds) }}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode="dark"
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        maxZoom={2.5}
        snapToGrid={snapOn}
        snapGrid={[GRID, GRID]}
        deleteKeyCode={null}
        multiSelectionKeyCode={['Shift', 'Meta', 'Control']}
        selectionKeyCode="Shift"
        nodesDraggable={!liveMode && tool === 'select'}
        nodesConnectable={connectMode}
        elementsSelectable={!liveMode && tool === 'select'}
        proOptions={{ hideAttribution: true }}
      >
        {gridOn && <Background variant={BackgroundVariant.Dots} gap={26} size={1} color="rgba(148,163,184,0.18)" />}
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => STATUS_HEX[statusKey((n.data as any)?.node?.status)] || STATUS_HEX.unknown}
          nodeStrokeWidth={2}
          maskColor="rgba(2,6,23,0.6)"
          className="!bg-surface"
        />

        {/* Floating EVE-style toolbar */}
        {!liveMode && (
          <Panel position="top-left">
            <div className="flex items-center gap-1 rounded-lg border border-border bg-surface/90 p-1 shadow-lg backdrop-blur">
              <ToolBtn active={tool === 'select'} onClick={() => setTool('select')} title="Select (V)"><MousePointer2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={tool === 'connect'} onClick={() => setTool('connect')} title="Connect / draw cable (C)"><Cable className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn active={snapOn} onClick={() => setSnapOn((v) => !v)} title="Snap to grid"><Magnet className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={gridOn} onClick={() => setGridOn((v) => !v)} title="Show grid"><Grid3x3 className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={autoAlign} title="Auto-align all to grid"><Wand2 className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('left')} title="Align left"><AlignStartVertical className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('top')} title="Align top"><AlignStartHorizontal className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('center-h')} title="Center horizontally"><AlignHorizontalJustifyCenter className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 1} danger onClick={deleteSelected} title="Delete selected"><Trash2 className="h-4 w-4" /></ToolBtn>
              {selCount > 0 && <span className="px-1.5 text-[11px] font-semibold text-muted">{selCount} sel</span>}
            </div>
          </Panel>
        )}
      </ReactFlow>

      <ContextMenu state={menu} onClose={closeMenu} />

      <ConfirmDialog
        open={!!pendingDelete}
        onOpenChange={(o) => { if (!o) setPendingDelete(null) }}
        title={pendingDelete?.title || ''}
        description={pendingDelete?.description}
        confirmText="Remove"
        destructive
        onConfirm={() => { pendingDelete?.run(); setPendingDelete(null) }}
      />

      {pendingLink && (
        <LinkDialog
          source={pendingLink.source}
          target={pendingLink.target}
          saving={addLink.isPending}
          onCancel={() => setPendingLink(null)}
          onCreate={createLink}
        />
      )}
    </div>
    </MapModeContext.Provider>
  )
}

function ToolBtn({ children, onClick, active, disabled, danger, title }: {
  children: React.ReactNode
  onClick: () => void
  active?: boolean
  disabled?: boolean
  danger?: boolean
  title: string
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'flex h-7 w-7 items-center justify-center rounded-md transition',
        disabled ? 'cursor-not-allowed text-muted/40'
          : danger ? 'text-muted hover:bg-danger/10 hover:text-danger'
          : active ? 'bg-primary/15 text-primary'
          : 'text-muted hover:bg-primary/10 hover:text-text',
      )}
    >
      {children}
    </button>
  )
}
