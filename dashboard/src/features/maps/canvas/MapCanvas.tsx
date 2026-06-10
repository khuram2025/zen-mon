import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  ConnectionMode,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  SelectionMode,
  ViewportPortal,
  useEdgesState,
  useNodesState,
  useReactFlow,
  useStore,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  AlignHorizontalDistributeCenter,
  AlignHorizontalJustifyCenter,
  AlignStartHorizontal,
  AlignStartVertical,
  AlignVerticalDistributeCenter,
  Cable,
  Grid3x3,
  Magnet,
  MousePointer2,
  Pencil,
  Redo2,
  Ruler,
  Sparkles,
  Spline,
  Trash2,
  Undo2,
  Wand2,
  Zap,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { useTheme } from '@/stores/theme'
import {
  LOGICAL_H,
  LOGICAL_W,
  discCenterToNodeXY,
  nodeXYToDiscCenter,
  pctToPx,
  pxToPct,
  pxToShape,
  shapeToPx,
  statusKey,
  type AnnotationLink,
  type LiveLinkData,
  type ManualMapDetail,
  type ManualMapLink,
  type ManualMapNode,
  type MapShape,
  type NodeLiveData,
} from '../core'
import { useMapMutations } from '../useMapData'
import { DeviceNode } from './DeviceNode'
import { ShapeNode } from './ShapeNode'
import { NetworkEdge } from './NetworkEdge'
import { ContextMenu, type ContextMenuState, type MenuItem } from './ContextMenu'
import { computeAlign, snap, type AlignOp } from './align'
import { MapModeContext } from './MapModeContext'
import { LinkDialog, type NewLink } from './LinkDialog'
import { DEVICE_DND_TYPE } from './DevicePalette'
import { LinkEditDialog, NodeEditDialog } from './EditDialogs'
import { InsertMenu, ShapeInspector, type ShapeSpec } from './Annotations'
import { GroupResizer } from './GroupResizer'
import { MapLegend, NocStatusBar } from './NocOverlays'
import { NO_SNAP, computeSmartSnap, nodeToGuideBox, tidyLayout, type SmartSnapResult } from './smartGuides'

const nodeTypes = { device: DeviceNode, shape: ShapeNode }
const edgeTypes = { network: NetworkEdge }
const GRID = 40 // logical px

const STATUS_HEX: Record<string, string> = {
  up: '#22c55e', down: '#ef4444', degraded: '#f59e0b', maintenance: '#3b82f6', unknown: '#6b7280',
}

export type MapCanvasProps = {
  mapId: string
  detail: ManualMapDetail
  liveData: Record<string, LiveLinkData>
  nodesLive: Record<string, NodeLiveData>
  liveUpdatedAt: number
  liveMode: boolean
  showThroughput: boolean
}

export function MapCanvas({ mapId, detail, liveData, nodesLive, liveUpdatedAt, liveMode, showThroughput }: MapCanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [snapOn, setSnapOn] = useState(false)
  const [gridOn, setGridOn] = useState(true)
  const [smartOn, setSmartOn] = useState(true) // magnetic alignment guides
  const [guides, setGuides] = useState<SmartSnapResult>(NO_SNAP)
  const [menu, setMenu] = useState<ContextMenuState>(null)
  const [selCount, setSelCount] = useState(0)
  const [tool, setTool] = useState<'select' | 'connect'>('select')
  const [panKey, setPanKey] = useState(false) // Space held → temporarily pan with left-drag
  // All deletions are explicit + confirmed. Nothing deletes without this.
  const [pendingDelete, setPendingDelete] = useState<{ title: string; description: string; run: () => void } | null>(null)
  const [pendingLink, setPendingLink] = useState<{ source: ManualMapNode; target: ManualMapNode } | null>(null)
  const [editNode, setEditNode] = useState<ManualMapNode | null>(null)
  const [editLink, setEditLink] = useState<ManualMapLink | null>(null)
  const [dropActive, setDropActive] = useState(false)
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null)
  const [annLinks, setAnnLinks] = useState<AnnotationLink[]>([])
  const annLinksRef = useRef<AnnotationLink[]>([])
  useEffect(() => { annLinksRef.current = annLinks }, [annLinks])

  const connectMode = tool === 'connect' && !liveMode
  const theme = useTheme((s) => s.theme)
  const { bulkMove, deleteNode, deleteLink, updateLink, updateNode, addLink, addNode, addShape, updateShape, deleteShape, saveMapMeta, autoConnect } = useMapMutations(mapId)
  const { screenToFlowPosition } = useReactFlow()
  const rfTransform = useStore((s) => s.transform)
  const nodeById = useMemo(() => new Map(detail.nodes.map((n) => [n.id, n])), [detail.nodes])
  const selectedShape = useMemo<MapShape | null>(
    () => (selectedShapeId ? ((nodes.find((n) => n.id === selectedShapeId)?.data as any)?.shape ?? null) : null),
    [selectedShapeId, nodes],
  )

  // Mirrors so callbacks can read the latest local metadata.
  const edgesRef = useRef<Edge[]>([])
  useEffect(() => { edgesRef.current = edges }, [edges])
  const nodesRef = useRef<Node[]>([])
  useEffect(() => { nodesRef.current = nodes }, [nodes])
  // Lets the (stable) keyboard listener call the latest deleteSelected.
  const deleteSelectedRef = useRef<() => void>(() => {})

  /* ── Undo / redo (reversible edits: moves, device/shape/link edits) ──────
   * Each entry is an inverse-command pair. Apply helpers (defined later) are
   * referenced via ref so this can sit above them without ordering issues. */
  type HistEntry = { undo: () => void; redo: () => void }
  const undoStack = useRef<HistEntry[]>([])
  const redoStack = useRef<HistEntry[]>([])
  const pushHistory = useCallback((entry: HistEntry) => {
    undoStack.current.push(entry)
    if (undoStack.current.length > 100) undoStack.current.shift()
    redoStack.current = []
  }, [])
  const undo = useCallback(() => {
    const e = undoStack.current.pop()
    if (!e) { toast.info('Nothing to undo'); return }
    e.undo(); redoStack.current.push(e); toast.success('Undone')
  }, [])
  const redo = useCallback(() => {
    const e = redoStack.current.pop()
    if (!e) { toast.info('Nothing to redo'); return }
    e.redo(); undoStack.current.push(e); toast.success('Redone')
  }, [])
  // New map / refetch clears history (ids/positions may have changed server-side).
  useEffect(() => { undoStack.current = []; redoStack.current = [] }, [mapId])

  /* ── Smart alignment guides ──────────────────────────────────────────────
   * Intercept single-node drags and magnetically snap to other nodes'
   * centres/edges and to equal row/column spacing, rendering pink guide
   * lines while snapped. Grid snap (snapOn) takes precedence when enabled. */
  const onNodesChangeSmart = useCallback((changes: NodeChange[]) => {
    if (!liveMode && smartOn && !snapOn) {
      // Snap every RF-driven position change — including the final one on
      // drop (dragging=false), which carries the raw pointer position and
      // would otherwise discard the magnetic correction.
      const dragging = changes.filter((c): c is Extract<NodeChange, { type: 'position' }> => c.type === 'position' && !!c.position)
      if (dragging.length === 1) {
        const ch = dragging[0]
        const dragNode = nodesRef.current.find((n) => n.id === ch.id)
        if (dragNode) {
          const tol = 7 / Math.max(0.3, rfTransform[2])
          const dragBox = { ...nodeToGuideBox(dragNode), x: ch.position!.x, y: ch.position!.y }
          const others = nodesRef.current.filter((n) => n.id !== ch.id && !n.selected).map(nodeToGuideBox)
          const res = computeSmartSnap(dragBox, others, tol)
          ch.position = { x: ch.position!.x + res.dx, y: ch.position!.y + res.dy }
          setGuides(res)
        }
      } else if (changes.some((c) => c.type === 'position')) {
        setGuides(NO_SNAP) // group drags don't guide (too noisy)
      }
    }
    onNodesChange(changes)
  }, [liveMode, smartOn, snapOn, rfTransform, onNodesChange])

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
      const ed = edgesRef.current.find((e) => e.id === linkId)
      const cur = (ed?.data as any)?.link
      const priorMeta = { ...(cur?.metadata || {}) }
      const meta = { ...(cur?.metadata || {}), ...patch }
      if ((ed?.data as any)?.annotation) persistAnnLinks(annArrayFromEdges(linkId, meta))
      else updateLink.mutate({ id: linkId, patch: { metadata: meta } }, { onError: () => toast.error('Failed to save link') })
      pushHistory({ undo: () => applyLinkMetaFull(linkId, priorMeta), redo: () => applyLinkMetaFull(linkId, meta) })
    }
  }

  const setEdgeWaypoints = useCallback((linkId: string, wpsPx: { x: number; y: number }[], commit: boolean) => {
    patchLinkMetaRef.current(linkId, { waypoints: wpsPx.map((p) => pxToPct(p.x, p.y)) }, commit)
  }, [])

  const setEdgeShape = useCallback((linkId: string, shape: 'curve' | 'straight' | 'orthogonal') => {
    // "Straight" means a direct line, so drop any existing bends — otherwise the
    // line would still route through old waypoints and look curved/kinked.
    // (You can still add fresh bends afterwards.)
    patchLinkMetaRef.current(linkId, shape === 'straight' ? { shape, waypoints: [] } : { shape }, true)
  }, [])

  // Move/rotate a port (interface) label. `which` is the source or target chip.
  const setIfacePos = useCallback((linkId: string, which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => {
    const cur = (edgesRef.current.find((e) => e.id === linkId)?.data as any)?.link?.metadata?.iface_pos || {}
    patchLinkMetaRef.current(linkId, { iface_pos: { ...cur, [which]: { ...(cur[which] || {}), ...pos } } }, commit)
  }, [])

  const resetIfaceLabels = useCallback((linkId: string) => {
    patchLinkMetaRef.current(linkId, { iface_pos: {} }, true)
  }, [])

  /* ── Annotation shape editing (ref-stable, like the others) ──────────────
   * Patches the local shape node immediately and (on commit) persists. */
  const patchShapeRef = useRef<(shapeId: string, patch: Record<string, unknown>, commit: boolean) => void>(() => {})
  patchShapeRef.current = (shapeId, patch, commit) => {
    const before = commit ? ((nodesRef.current.find((n) => n.id === shapeId && n.type === 'shape')?.data as any)?.shape as MapShape | undefined) : undefined
    setNodes((nds) => nds.map((n) => {
      if (n.id !== shapeId || n.type !== 'shape') return n
      const sh = (n.data as any).shape as MapShape
      const meta = patch.metadata ? { ...(sh.metadata || {}), ...(patch.metadata as object) } : sh.metadata
      return { ...n, data: { ...n.data, shape: { ...sh, ...patch, metadata: meta } } }
    }))
    if (commit) {
      updateShape.mutate({ id: shapeId, patch }, { onError: () => toast.error('Failed to save annotation') })
      if (before) {
        const priorFields = { text: before.text ?? null, fill: before.fill ?? null, stroke: before.stroke ?? null, metadata: before.metadata || {} }
        const nextFields = { ...priorFields, ...patch, metadata: patch.metadata ? { ...(before.metadata || {}), ...(patch.metadata as object) } : (before.metadata || {}) }
        pushHistory({ undo: () => applyShapeFields(shapeId, priorFields), redo: () => applyShapeFields(shapeId, nextFields) })
      }
    }
  }
  const setShape = useCallback((shapeId: string, patch: Record<string, unknown>, commit: boolean) => {
    patchShapeRef.current(shapeId, patch, commit)
  }, [])

  // Load annotation links (cables touching icons/images) from the map metadata.
  useEffect(() => {
    const al = ((detail.metadata as any)?.annotation_links as AnnotationLink[]) || []
    setAnnLinks(al)
  }, [detail.metadata])

  /** Persist the annotation_links array into the map metadata (no refetch). */
  const persistAnnLinks = useCallback((arr: AnnotationLink[]) => {
    saveMapMeta.mutate({ ...((detail.metadata as any) || {}), annotation_links: arr }, { onError: () => toast.error('Failed to save link') })
  }, [saveMapMeta, detail.metadata])

  // Rebuild the array from current edge state (captures live shape/bend edits),
  // optionally overriding one edge's metadata to beat the edgesRef lag.
  const annArrayFromEdges = useCallback((overrideId?: string, overrideMeta?: Record<string, unknown>): AnnotationLink[] => {
    return edgesRef.current.filter((e) => (e.data as any)?.annotation).map((e) => {
      const dd = e.data as any
      return {
        id: e.id, source: e.source, target: e.target,
        source_type: dd.sourceType, target_type: dd.targetType,
        label: dd.link?.label ?? null, link_type: dd.link?.link_type || 'manual',
        metadata: e.id === overrideId ? (overrideMeta as any) : (dd.link?.metadata || {}),
      }
    })
  }, [])

  const bumpShapeZ = useCallback((shapeId: string, dir: 'front' | 'back') => {
    const zs = nodesRef.current.filter((n) => n.type === 'shape').map((n) => (n.data as any).shape.z_index || 0)
    const z = dir === 'front' ? Math.max(0, ...zs) + 1 : Math.min(0, ...zs) - 1
    patchShapeRef.current(shapeId, { z_index: z }, true)
    setNodes((nds) => nds.map((n) => (n.id === shapeId ? { ...n, zIndex: z } : n)))
  }, [setNodes])

  // Preserve selection across server-driven rebuilds.
  const selectedIds = useRef<Set<string>>(new Set())

  useEffect(() => {
    const deviceNodes: Node[] = detail.nodes.map((n) => {
      const c = pctToPx(n)
      return {
        id: n.id,
        type: 'device',
        position: discCenterToNodeXY(c.x, c.y),
        data: {
          node: n,
          live: liveMode,
          nodeLive: liveMode ? nodesLive[n.id] : undefined,
          onLabelMove: (dx: number, dy: number, commit: boolean) => setNodeLabelOffset(n.id, dx, dy, commit),
        },
        draggable: !liveMode,
        selected: selectedIds.current.has(n.id),
        // RF turns pointer events off for non-interactive nodes; live mode
        // still needs hover for the health card.
        style: liveMode ? { pointerEvents: 'all' as const } : undefined,
      }
    })
    // Annotation shapes render UNDER the devices (lower zIndex) unless their own
    // z_index pushes them up; devices/links stay clickable on top by default.
    const shapeNodes: Node[] = (detail.shapes || []).map((s) => {
      const r = shapeToPx(s)
      return {
        id: s.id,
        type: 'shape',
        position: { x: r.x, y: r.y },
        width: Math.round(r.w),
        height: Math.round(r.h),
        style: { width: Math.round(r.w), height: Math.round(r.h) },
        data: {
          shape: s,
          live: liveMode,
          onEditText: (text: string) => setShape(s.id, { text }, true),
          onResizeEnd: (rect: { x: number; y: number; width: number; height: number }) =>
            setShape(s.id, pxToShape(rect.x, rect.y, rect.width, rect.height), true),
        },
        draggable: !liveMode,
        selected: selectedIds.current.has(s.id),
        zIndex: s.z_index || 0,
      }
    })
    setNodes([...shapeNodes, ...deviceNodes])
  }, [detail.nodes, detail.shapes, liveMode, nodesLive, setNodes, setNodeLabelOffset, setShape])

  useEffect(() => {
    const visible = detail.links.filter((l) => nodeById.has(l.source_node_id) && nodeById.has(l.target_node_id))

    // Spread multiple links between the SAME device pair so they don't overlap:
    // each gets a perpendicular offset centred on the direct line.
    const PARALLEL_GAP = 18 // logical px between adjacent parallel cables
    const groups = new Map<string, string[]>()
    for (const l of visible) {
      const key = [l.source_node_id, l.target_node_id].sort().join('|')
      const arr = groups.get(key) || []
      arr.push(l.id)
      groups.set(key, arr)
    }
    const offsetById = new Map<string, number>()
    for (const ids of groups.values()) {
      const n = ids.length
      ids.forEach((id, i) => offsetById.set(id, (i - (n - 1) / 2) * PARALLEL_GAP))
    }

    const deviceEdges: Edge[] = visible.map((l) => {
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
          // The 15s live status feed wins over the map snapshot when present.
          sourceStatus: (liveMode && nodesLive[l.source_node_id]?.status) || s.status,
          targetStatus: (liveMode && nodesLive[l.target_node_id]?.status) || t.status,
          live: liveData[l.id],
          liveMode,
          showThroughput,
          parallelOffset: offsetById.get(l.id) || 0,
          setWaypoints: (wpsPx: { x: number; y: number }[], commit: boolean) => setEdgeWaypoints(l.id, wpsPx, commit),
          setIfacePos: (which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => setIfacePos(l.id, which, pos, commit),
        },
      }
    })

    // Annotation cables (touch an icon/image). Shape endpoints have no status →
    // treat as 'up' so the cable looks healthy; a device endpoint keeps its real
    // status so a down device still colours the link red.
    const statusFor = (id: string, kind: 'node' | 'shape') => (kind === 'node' ? (nodeById.get(id)?.status || 'unknown') : 'up')
    const shapeIds = new Set((detail.shapes || []).map((s) => s.id))
    const annEdges: Edge[] = annLinks
      .filter((al) => (nodeById.has(al.source) || shapeIds.has(al.source)) && (nodeById.has(al.target) || shapeIds.has(al.target)))
      .map((al) => ({
        id: al.id,
        source: al.source,
        target: al.target,
        sourceHandle: 'c',
        targetHandle: 'c',
        type: 'network',
        data: {
          link: { id: al.id, source_node_id: al.source, target_node_id: al.target, label: al.label ?? null, link_type: al.link_type || 'manual', metadata: al.metadata || {} },
          sourceStatus: statusFor(al.source, al.source_type),
          targetStatus: statusFor(al.target, al.target_type),
          live: undefined,
          liveMode,
          showThroughput,
          annotation: true,
          sourceType: al.source_type,
          targetType: al.target_type,
          parallelOffset: 0,
          setWaypoints: (wpsPx: { x: number; y: number }[], commit: boolean) => setEdgeWaypoints(al.id, wpsPx, commit),
        },
      }))

    setEdges([...deviceEdges, ...annEdges])
  }, [detail.links, detail.shapes, annLinks, liveData, liveMode, nodesLive, showThroughput, nodeById, setEdges, setEdgeWaypoints, setIfacePos])

  /* ── Persistence ─────────────────────────────────────────────── */
  const persistPositions = useCallback((items: { id: string; x: number; y: number }[]) => {
    const payload = items.map((it) => {
      const c = nodeXYToDiscCenter(it.x, it.y)
      const p = pxToPct(c.x, c.y)
      return { id: it.id, x_pct: p.x_pct, y_pct: p.y_pct }
    })
    bulkMove.mutate(payload, { onError: () => toast.error('Failed to save layout') })
  }, [bulkMove])

  /* ── Apply helpers (also used by undo/redo to re-apply a captured state) ── */
  // Restore node/shape positions (logical px top-left) locally + persist.
  const applyPositions = useCallback((moves: { id: string; x: number; y: number }[]) => {
    setNodes((nds) => nds.map((n) => {
      const m = moves.find((x) => x.id === n.id)
      return m ? { ...n, position: { x: m.x, y: m.y } } : n
    }))
    const byId = new Map(nodesRef.current.map((n) => [n.id, n]))
    const dev: { id: string; x: number; y: number }[] = []
    moves.forEach((m) => {
      const n = byId.get(m.id)
      if (!n) return
      if (n.type === 'shape') {
        const r = shapeToPx((n.data as any).shape)
        const p = pxToShape(m.x, m.y, r.w, r.h)
        updateShape.mutate({ id: m.id, patch: { x_pct: p.x_pct, y_pct: p.y_pct } })
      } else dev.push({ id: m.id, x: m.x, y: m.y })
    })
    if (dev.length) persistPositions(dev)
  }, [setNodes, persistPositions, updateShape])

  // When nodes move, carry the bends of links BETWEEN two moved nodes along by
  // the same delta — otherwise waypoints (stored as absolute canvas %) stay put
  // and the cable distorts. `deltas` maps node id → px shift.
  const shiftConnectedWaypoints = useCallback((deltas: Map<string, { dx: number; dy: number }>) => {
    const updates: { id: string; meta: Record<string, unknown> }[] = []
    edgesRef.current.forEach((e) => {
      if ((e.data as any).annotation) return
      const ds = deltas.get(e.source)
      if (!ds || !deltas.get(e.target)) return // both endpoints must have moved
      const link = (e.data as any).link
      const wps = link?.metadata?.waypoints
      if (!Array.isArray(wps) || !wps.length) return
      const ddx = (ds.dx / LOGICAL_W) * 100, ddy = (ds.dy / LOGICAL_H) * 100
      const nw = wps.map((w: any) => ({ x_pct: Math.max(0, Math.min(100, w.x_pct + ddx)), y_pct: Math.max(0, Math.min(100, w.y_pct + ddy)) }))
      updates.push({ id: e.id, meta: { ...(link.metadata || {}), waypoints: nw } })
    })
    if (!updates.length) return
    setEdges((eds) => eds.map((e) => {
      const u = updates.find((x) => x.id === e.id)
      return u ? { ...e, data: { ...e.data, link: { ...(e.data as any).link, metadata: u.meta } } } : e
    }))
    updates.forEach((u) => updateLink.mutate({ id: u.id, patch: { metadata: u.meta } }))
  }, [setEdges, updateLink])

  // Replace a device node's editable fields (label/icon/metadata) locally + persist.
  const applyNodeFields = useCallback((id: string, fields: Record<string, unknown>) => {
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, node: { ...(n.data as any).node, ...fields } } } : n)))
    updateNode.mutate({ id, patch: fields }, { onError: () => toast.error('Failed to save device') })
  }, [setNodes, updateNode])

  // Replace a shape's editable fields locally + persist.
  const applyShapeFields = useCallback((id: string, fields: Record<string, unknown>) => {
    setNodes((nds) => nds.map((n) => {
      if (n.id !== id || n.type !== 'shape') return n
      const sh = { ...(n.data as any).shape, ...fields }
      const r = shapeToPx(sh)
      return { ...n, data: { ...n.data, shape: sh }, position: { x: r.x, y: r.y }, width: Math.round(r.w), height: Math.round(r.h), style: { width: Math.round(r.w), height: Math.round(r.h) } }
    }))
    updateShape.mutate({ id, patch: fields }, { onError: () => toast.error('Failed to save annotation') })
  }, [setNodes, updateShape])

  // Replace a link's metadata (device or annotation) locally + persist.
  const applyLinkMetaFull = useCallback((id: string, metaFull: Record<string, unknown>) => {
    let annotation = false
    setEdges((eds) => eds.map((e) => {
      if (e.id !== id) return e
      if ((e.data as any).annotation) annotation = true
      return { ...e, data: { ...e.data, link: { ...(e.data as any).link, metadata: metaFull } } }
    }))
    if (annotation) persistAnnLinks(annArrayFromEdges(id, metaFull))
    else updateLink.mutate({ id, patch: { metadata: metaFull } }, { onError: () => toast.error('Failed to save link') })
  }, [setEdges, updateLink, persistAnnLinks, annArrayFromEdges])

  // Drag-to-connect. Device↔device opens the interface dialog (real link).
  // If either end is an icon/image annotation, create an annotation cable
  // directly (no interfaces) and persist it into the map metadata.
  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return
    const s = nodeById.get(c.source)
    const t = nodeById.get(c.target)
    if (s && t) { setPendingLink({ source: s, target: t }); return }
    const al: AnnotationLink = {
      // crypto.randomUUID needs a secure context (https/localhost); the app is
      // served over plain http, so use a timestamp+random id instead.
      id: `al-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`,
      source: c.source, target: c.target,
      source_type: nodeById.has(c.source) ? 'node' : 'shape',
      target_type: nodeById.has(c.target) ? 'node' : 'shape',
      label: null, link_type: 'manual',
      metadata: { kind: 'ethernet', shape: 'straight' },
    }
    const next = [...annLinksRef.current, al]
    setAnnLinks(next)
    persistAnnLinks(next)
    toast.success('Link created')
  }, [nodeById, persistAnnLinks])

  const deleteAnnotationLink = useCallback((id: string) => {
    const next = annLinksRef.current.filter((a) => a.id !== id)
    setAnnLinks(next)
    persistAnnLinks(next)
  }, [persistAnnLinks])

  const createLink = useCallback((link: NewLink) => {
    addLink.mutate(link, {
      onSuccess: () => { setPendingLink(null); toast.success('Link created') },
      onError: (e: any) => {
        // Keep the dialog open so the user can adjust; surface a clear message.
        toast.error(e?.response?.status === 409 ? 'A link between these two devices already exists.' : 'Failed to create link')
      },
    })
  }, [addLink])

  /* ── Drop a device from the palette → place it as a node ─────────
   * screenToFlowPosition gives the flow-space point under the cursor, which is
   * exactly where we want the disc centre → convert straight to percent. */
  const onCanvasDragOver = useCallback((e: React.DragEvent) => {
    if (liveMode || !e.dataTransfer.types.includes(DEVICE_DND_TYPE)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    if (!dropActive) setDropActive(true)
  }, [liveMode, dropActive])

  const onCanvasDrop = useCallback((e: React.DragEvent) => {
    setDropActive(false)
    if (liveMode) return
    const deviceId = e.dataTransfer.getData(DEVICE_DND_TYPE)
    if (!deviceId) return
    e.preventDefault()
    if (detail.nodes.some((n) => n.device_id === deviceId)) {
      toast.info('Already on this map')
      return
    }
    const center = screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const p = pxToPct(center.x, center.y)
    addNode.mutate(
      { device_id: deviceId, x_pct: p.x_pct, y_pct: p.y_pct },
      { onSuccess: () => toast.success('Device placed'), onError: () => toast.error('Failed to place device') },
    )
  }, [liveMode, detail.nodes, screenToFlowPosition, addNode])

  /* ── Edit (label/icon for nodes, label/type/shape/interfaces for links) ──
   * updateNode/updateLink don't refetch, so we patch local RF state too. */
  const saveNode = useCallback((id: string, patch: { label: string | null; icon: string; metadata?: Record<string, unknown> }) => {
    const before = (nodesRef.current.find((n) => n.id === id)?.data as any)?.node
    if (before) {
      const prior = { label: before.label ?? null, icon: before.icon, metadata: before.metadata || {} }
      pushHistory({ undo: () => applyNodeFields(id, prior), redo: () => applyNodeFields(id, patch) })
    }
    setNodes((nds) => nds.map((n) => (n.id === id
      ? { ...n, data: { ...n.data, node: { ...(n.data as any).node, ...patch } } }
      : n)))
    updateNode.mutate({ id, patch }, {
      onSuccess: () => { setEditNode(null); toast.success('Device updated') },
      onError: () => toast.error('Failed to save device'),
    })
  }, [setNodes, updateNode])

  const saveLink = useCallback((id: string, patch: { label: string | null; link_type: string; metadata: Record<string, unknown> }) => {
    setEdges((eds) => eds.map((e) => (e.id === id
      ? { ...e, data: { ...e.data, link: { ...(e.data as any).link, ...patch } } }
      : e)))
    updateLink.mutate({ id, patch }, {
      onSuccess: () => { setEditLink(null); toast.success('Link updated') },
      onError: () => toast.error('Failed to save link'),
    })
  }, [setEdges, updateLink])

  // Capture pre-drag positions so the move can be undone.
  const dragPrevRef = useRef<{ id: string; x: number; y: number }[]>([])
  const onNodeDragStart = useCallback((_e: unknown, node: Node, dragged: Node[]) => {
    const moving = dragged && dragged.length ? dragged : [node]
    dragPrevRef.current = moving.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
  }, [])
  const onNodeDragStop = useCallback((_e: unknown, node: Node, dragged: Node[]) => {
    setGuides(NO_SNAP)
    // Read final positions from the store, not the event args — the event
    // carries the raw pointer-derived position, losing the magnetic snap.
    const moved = (dragged && dragged.length ? dragged : [node]).map((n) => nodesRef.current.find((x) => x.id === n.id) || n)
    const next = moved.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    applyPositions(next)
    const prev = dragPrevRef.current
    // Carry along the bends of cables between two moved nodes.
    const deltas = new Map<string, { dx: number; dy: number }>()
    next.forEach((nx) => { const p = prev.find((q) => q.id === nx.id); if (p) deltas.set(nx.id, { dx: nx.x - p.x, dy: nx.y - p.y }) })
    shiftConnectedWaypoints(deltas)
    if (prev.length && prev.some((p, i) => p.x !== next[i]?.x || p.y !== next[i]?.y)) {
      pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
    }
  }, [applyPositions, shiftConnectedWaypoints, pushHistory])

  // Quick-connect: auto-draw CDP/LLDP links among the placed devices.
  const quickConnect = useCallback(() => {
    if (autoConnect.isPending) return
    autoConnect.mutate(undefined, {
      onSuccess: (r) => toast.success(r.created > 0 ? `Connected ${r.created} link${r.created > 1 ? 's' : ''} from CDP/LLDP` : 'No new discovered links found'),
      onError: () => toast.error('Quick connect failed'),
    })
  }, [autoConnect])

  /* ── Insert an annotation shape at the current viewport centre ───────── */
  const addShapeAt = useCallback((spec: ShapeSpec) => {
    const rect = wrapperRef.current?.getBoundingClientRect()
    const cx = rect ? rect.left + rect.width / 2 : 400
    const cy = rect ? rect.top + rect.height / 2 : 300
    const c = screenToFlowPosition({ x: cx, y: cy })
    const w_pct = spec.w_pct ?? 12, h_pct = spec.h_pct ?? 8
    const wPx = (w_pct / 100) * LOGICAL_W, hPx = (h_pct / 100) * LOGICAL_H
    const pct = pxToShape(c.x - wPx / 2, c.y - hPx / 2, wPx, hPx)
    addShape.mutate({
      kind: spec.kind, x_pct: pct.x_pct, y_pct: pct.y_pct, w_pct, h_pct,
      text: spec.text ?? null, fill: spec.fill ?? null, stroke: spec.stroke ?? null,
      metadata: spec.metadata ?? {},
    }, { onSuccess: () => toast.success('Annotation added'), onError: () => toast.error('Failed to add annotation') })
  }, [screenToFlowPosition, addShape])

  // NOTE: we intentionally do NOT wire RF's onNodesDelete/onEdgesDelete to the
  // backend, and deleteKeyCode is disabled — every deletion goes through an
  // explicit ConfirmDialog so nothing can ever be removed implicitly.

  /* ── Alignment / auto-align ──────────────────────────────────── */
  const applyAlign = useCallback((op: AlignOp) => {
    const sel = nodes.filter((n) => n.selected && n.type !== 'shape')
    if (sel.length < 2) return
    const prev = sel.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const moves = computeAlign(sel, op)
    const next = sel.map((n) => ({ id: n.id, ...(moves.get(n.id) || n.position) }))
    applyPositions(next)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
  }, [nodes, applyPositions, pushHistory])

  const autoAlign = useCallback(() => {
    const all = nodes.filter((n) => n.type !== 'shape')
    if (!all.length) return
    const prev = all.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const next = all.map((n) => ({ id: n.id, x: snap(n.position.x, GRID), y: snap(n.position.y, GRID) }))
    applyPositions(next)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
    toast.success('Aligned all to grid')
  }, [nodes, applyPositions, pushHistory])

  // One-click tidy: straighten near-aligned rows/columns and even out their
  // spacing — turns a roughly-placed sketch into a clean diagram (undoable).
  const tidyUp = useCallback(() => {
    const moves = tidyLayout(nodesRef.current)
    if (!moves.size) { toast.info('Layout is already tidy'); return }
    const prev = nodesRef.current.filter((n) => moves.has(n.id)).map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const next = [...moves].map(([id, p]) => ({ id, ...p }))
    applyPositions(next)
    const deltas = new Map<string, { dx: number; dy: number }>()
    next.forEach((m) => { const p = prev.find((q) => q.id === m.id); if (p) deltas.set(m.id, { dx: m.x - p.x, dy: m.y - p.y }) })
    shiftConnectedWaypoints(deltas)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
    toast.success(`Tidied ${next.length} node${next.length > 1 ? 's' : ''}`)
  }, [applyPositions, shiftConnectedWaypoints, pushHistory])

  // Copy one node's disc size to every selected device.
  const matchSizes = useCallback((refNode: ManualMapNode) => {
    const scale = refNode.metadata?.size_scale || 1
    const ids = nodesRef.current.filter((n) => n.selected && n.type === 'device' && n.id !== refNode.id).map((n) => n.id)
    ids.forEach((id) => patchNodeMetaRef.current(id, { size_scale: scale }, true))
    if (ids.length) toast.success(`Matched ${ids.length} device size${ids.length > 1 ? 's' : ''}`)
  }, [])

  // Snap every selected device's label back under its disc.
  const resetLabels = useCallback(() => {
    const ids = nodesRef.current.filter((n) => n.selected && n.type === 'device').map((n) => n.id)
    ids.forEach((id) => patchNodeMetaRef.current(id, { label_offset: { dx: 0, dy: 0 } }, true))
    if (ids.length) toast.success('Label positions reset')
  }, [])

  /* ── Selection tracking + keyboard ───────────────────────────── */
  const onSelectionChange = useCallback(({ nodes: sel }: { nodes: Node[] }) => {
    selectedIds.current = new Set(sel.map((n) => n.id))
    setSelCount(sel.length)
    // Show the style inspector only when exactly one annotation shape is selected.
    const shapeSel = sel.filter((n) => n.type === 'shape')
    setSelectedShapeId(sel.length === 1 && shapeSel.length === 1 ? shapeSel[0].id : null)
  }, [])

  const wrapperRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = wrapperRef.current
    if (!el) return
    const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') setPanKey(false) }
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.code === 'Space') {
        e.preventDefault()
        setPanKey(true)
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault()
        if (e.shiftKey) redo(); else undo()
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault()
        redo()
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
        e.preventDefault()
        setNodes((nds) => nds.map((n) => ({ ...n, selected: true })))
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && !liveMode) {
        // Confirm-gated (deleteSelected opens a ConfirmDialog) — never implicit.
        e.preventDefault()
        deleteSelectedRef.current()
      } else if (!e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'v') {
        setTool('select')
      } else if (!e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'c') {
        setTool('connect')
      }
    }
    el.addEventListener('keydown', onKey)
    el.addEventListener('keyup', onKeyUp)
    return () => { el.removeEventListener('keydown', onKey); el.removeEventListener('keyup', onKeyUp) }
  }, [setNodes, liveMode, undo, redo])

  /* ── Context menus ───────────────────────────────────────────── */
  const closeMenu = useCallback(() => setMenu(null), [])

  const onPaneContextMenu = useCallback((e: MouseEvent | React.MouseEvent) => {
    e.preventDefault()
    const sel = nodesRef.current.filter((n) => n.selected).length
    const items: MenuItem[] = [{ type: 'header', label: sel > 0 ? `${sel} selected` : 'Canvas' }]
    if (sel > 0) {
      items.push({ type: 'item', label: `Delete selected (${sel})`, icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => deleteSelected() })
      if (sel >= 2) {
        items.push({ type: 'item', label: 'Align left', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => applyAlign('left') })
        items.push({ type: 'item', label: 'Align top', icon: <AlignStartHorizontal className="h-4 w-4" />, onClick: () => applyAlign('top') })
      }
      items.push({ type: 'divider' })
    }
    items.push({ type: 'item', label: 'Select all', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => setNodes((nds) => nds.map((n) => ({ ...n, selected: true }))) })
    items.push({ type: 'item', label: 'Auto-align to grid', icon: <Wand2 className="h-4 w-4" />, onClick: autoAlign })
    items.push({ type: 'item', label: 'Tidy layout', icon: <Sparkles className="h-4 w-4" />, onClick: tidyUp })
    setMenu({ x: (e as MouseEvent).clientX, y: (e as MouseEvent).clientY, items })
  }, [setNodes, autoAlign, applyAlign, tidyUp])

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
      items.push({ type: 'item', label: 'Distribute horizontally', icon: <AlignHorizontalDistributeCenter className="h-4 w-4" />, onClick: () => applyAlign('distribute-h') })
      items.push({ type: 'item', label: 'Distribute vertically', icon: <AlignVerticalDistributeCenter className="h-4 w-4" />, onClick: () => applyAlign('distribute-v') })
      items.push({ type: 'divider' })
      if (dev) items.push({ type: 'item', label: 'Match sizes to this device', icon: <Sparkles className="h-4 w-4" />, onClick: () => matchSizes(dev) })
      items.push({ type: 'item', label: 'Reset label positions', onClick: () => resetLabels() })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Delete selected', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => deleteSelected() })
    } else if (node.type === 'shape') {
      items.push({ type: 'header', label: 'Annotation' })
      items.push({ type: 'item', label: 'Bring to front', onClick: () => bumpShapeZ(node.id, 'front') })
      items.push({ type: 'item', label: 'Send to back', onClick: () => bumpShapeZ(node.id, 'back') })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Delete', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: 'Delete annotation?',
        description: 'This removes the annotation from the map.',
        run: () => { deleteShape.mutate(node.id); selectedIds.current.delete(node.id); setNodes((nds) => nds.filter((n) => n.id !== node.id)) },
      }) })
    } else {
      items.push({ type: 'header', label: dev?.hostname || 'Node' })
      items.push({ type: 'item', label: 'Edit label & icon…', icon: <Pencil className="h-4 w-4" />, onClick: () => setEditNode(dev) })
      if (dev?.device_id) items.push({ type: 'item', label: 'Open device', onClick: () => window.open(`/devices/${dev.device_id}`, '_blank') })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Remove from map', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: 'Remove node?',
        description: `Remove "${dev?.hostname || 'this node'}" from this map? The device itself is not affected.`,
        run: () => { deleteNode.mutate(node.id); setNodes((nds) => nds.filter((n) => n.id !== node.id)) },
      }) })
    }
    setMenu({ x: e.clientX, y: e.clientY, items })
  }, [setNodes, applyAlign, deleteNode, deleteShape, bumpShapeZ, matchSizes, resetLabels])

  const onEdgeContextMenu = useCallback((e: React.MouseEvent, edge: Edge) => {
    e.preventDefault()
    const dd = edge.data as any
    const link = dd.link
    const isAnnotation = !!dd.annotation
    const shape = link?.metadata?.shape || 'curve'
    const shapeItem = (label: string, val: 'curve' | 'straight' | 'orthogonal'): MenuItem =>
      ({ type: 'item', label: `${shape === val ? '✓ ' : '   '}${label}`, onClick: () => setEdgeShape(edge.id, val) })
    const items: MenuItem[] = [
      { type: 'header', label: link?.label || (isAnnotation ? 'Connection' : 'Link') },
      { type: 'item', label: 'Edit link…', icon: <Pencil className="h-4 w-4" />, onClick: () => setEditLink(link) },
      { type: 'divider' },
      { type: 'header', label: 'Shape' },
      shapeItem('Curved', 'curve'),
      shapeItem('Straight', 'straight'),
      shapeItem('Orthogonal', 'orthogonal'),
      { type: 'divider' },
      { type: 'item', label: 'Reset bends', icon: <Spline className="h-4 w-4" />, onClick: () => setEdgeWaypoints(edge.id, [], true) },
      { type: 'item', label: 'Reset port labels', onClick: () => resetIfaceLabels(edge.id) },
      { type: 'divider' },
      { type: 'item', label: isAnnotation ? 'Delete connection' : 'Delete link', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: isAnnotation ? 'Delete connection?' : 'Delete link?',
        description: 'This removes the connection between the two items.',
        run: () => { if (isAnnotation) deleteAnnotationLink(edge.id); else { deleteLink.mutate(edge.id); setEdges((eds) => eds.filter((x) => x.id !== edge.id)) } },
      }) },
    ]
    const shown = isAnnotation ? items.filter((it) => it.type !== 'item' || (it.label !== 'Edit link…' && it.label !== 'Reset port labels')) : items
    setMenu({ x: e.clientX, y: e.clientY, items: shown })
  }, [setEdgeShape, setEdgeWaypoints, resetIfaceLabels, deleteLink, deleteAnnotationLink, setEdges])

  // Explicit edge selection (RF's built-in click-select is unreliable for our
  // fully-custom edge); selecting an edge reveals its bend handles.
  // Double-click a device tile to open its editor (label text, colour, font,
  // size, icon, container size). Shapes handle their own double-click (text).
  const onNodeDoubleClick = useCallback((_e: React.MouseEvent, node: Node) => {
    if (node.type === 'device' && !liveMode) setEditNode((node.data as any).node)
  }, [liveMode])

  const onEdgeClick = useCallback((_e: React.MouseEvent, edge: Edge) => {
    setEdges((eds) => eds.map((x) => (x.selected === (x.id === edge.id) ? x : { ...x, selected: x.id === edge.id })))
    setNodes((nds) => nds.some((n) => n.selected) ? nds.map((n) => (n.selected ? { ...n, selected: false } : n)) : nds)
  }, [setEdges, setNodes])

  /* ── Group resize (scale spacing of the marquee selection) ───────────── */
  const groupItems = useMemo(
    () => nodes.filter((n) => n.selected).map((n) => ({
      id: n.id, x: n.position.x, y: n.position.y,
      w: typeof n.width === 'number' ? n.width : 128,
      h: typeof n.height === 'number' ? n.height : 64,
    })),
    [nodes],
  )
  const groupPrevRef = useRef<{ id: string; x: number; y: number }[] | null>(null)
  const applyGroup = useCallback((moves: { id: string; x: number; y: number }[], commit: boolean) => {
    // Snapshot starting positions on the first frame of the gesture (for undo).
    if (!groupPrevRef.current) {
      groupPrevRef.current = nodesRef.current.filter((n) => moves.some((m) => m.id === n.id)).map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    }
    setNodes((nds) => nds.map((n) => {
      const m = moves.find((x) => x.id === n.id)
      return m ? { ...n, position: { x: m.x, y: m.y } } : n
    }))
    if (!commit) return
    applyPositions(moves)
    const prev = groupPrevRef.current
    groupPrevRef.current = null
    if (prev) {
      const deltas = new Map<string, { dx: number; dy: number }>()
      moves.forEach((m) => { const p = prev.find((q) => q.id === m.id); if (p) deltas.set(m.id, { dx: m.x - p.x, dy: m.y - p.y }) })
      shiftConnectedWaypoints(deltas)
      pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(moves) })
    }
  }, [setNodes, applyPositions, shiftConnectedWaypoints, pushHistory])

  const deleteSelected = useCallback(() => {
    const sel = nodesRef.current.filter((n) => n.selected)
    if (!sel.length) return
    const shapes = sel.filter((n) => n.type === 'shape')
    const devices = sel.filter((n) => n.type !== 'shape')
    setPendingDelete({
      title: `Remove ${sel.length} item${sel.length > 1 ? 's' : ''}?`,
      description: `This removes ${sel.length === 1 ? 'it' : 'them'} from this map. Devices themselves are not affected.`,
      run: () => {
        devices.forEach((n) => { deleteNode.mutate(n.id); selectedIds.current.delete(n.id) })
        shapes.forEach((n) => { deleteShape.mutate(n.id); selectedIds.current.delete(n.id) })
        setNodes((nds) => nds.filter((n) => !n.selected))
        toast.success(`Removed ${sel.length} item${sel.length > 1 ? 's' : ''}`)
      },
    })
  }, [deleteNode, deleteShape, setNodes])
  deleteSelectedRef.current = deleteSelected

  return (
    <MapModeContext.Provider value={{ connectMode }}>
    <div
      ref={wrapperRef}
      tabIndex={0}
      onDragOver={onCanvasDragOver}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as HTMLElement | null)) setDropActive(false) }}
      onDrop={onCanvasDrop}
      className={cn('relative h-full w-full outline-none', connectMode && 'cursor-crosshair', panKey && 'cursor-grab')}
    >
      {dropActive && (
        <div className="pointer-events-none absolute inset-2 z-20 rounded-lg border-2 border-dashed border-primary/70 bg-primary/5" />
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChangeSmart}
        onEdgesChange={onEdgesChange}
        onNodeDragStart={onNodeDragStart}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        connectionMode={ConnectionMode.Loose}
        onSelectionChange={onSelectionChange}
        onPaneContextMenu={onPaneContextMenu}
        onNodeContextMenu={onNodeContextMenu}
        onNodeDoubleClick={onNodeDoubleClick}
        onEdgeContextMenu={onEdgeContextMenu}
        onEdgeClick={onEdgeClick}
        onPaneClick={() => { closeMenu(); setEdges((eds) => eds.some((e) => e.selected) ? eds.map((e) => ({ ...e, selected: false })) : eds) }}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode={theme}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        maxZoom={2.5}
        snapToGrid={snapOn}
        snapGrid={[GRID, GRID]}
        deleteKeyCode={null}
        multiSelectionKeyCode={['Shift', 'Meta', 'Control']}
        selectionKeyCode={null}
        selectionMode={SelectionMode.Partial}
        // In Select mode a plain left-drag on empty canvas draws a marquee to
        // box-select; hold Space (or use middle-mouse / the minimap) to pan.
        // Right button is deliberately NOT a pan trigger so right-click menus
        // keep working. Connect mode pans normally so it doesn't fight cabling.
        selectionOnDrag={!liveMode && tool === 'select' && !panKey}
        panOnDrag={(!liveMode && tool === 'select' && !panKey) ? [1] : true}
        nodesDraggable={!liveMode && tool === 'select'}
        nodesConnectable={connectMode}
        elementsSelectable={!liveMode && tool === 'select'}
        proOptions={{ hideAttribution: true }}
      >
        {gridOn && <Background variant={BackgroundVariant.Dots} gap={26} size={1} color={theme === 'dark' ? 'rgba(148,163,184,0.18)' : 'rgba(71,85,105,0.22)'} />}

        {/* Magnetic guide lines + equal-spacing hints (drawn in flow space) */}
        {(guides.v.length > 0 || guides.h.length > 0 || guides.gaps.length > 0) && (
          <ViewportPortal>
            <svg className="pointer-events-none absolute overflow-visible" style={{ left: 0, top: 0, zIndex: 1200 }} width={1} height={1}>
              {guides.v.map((g, i) => (
                <line key={`v${i}`} x1={g.coord} y1={g.from} x2={g.coord} y2={g.to} stroke="#ec4899" strokeWidth={1.4 / rfTransform[2]} strokeDasharray={`${5 / rfTransform[2]} ${4 / rfTransform[2]}`} />
              ))}
              {guides.h.map((g, i) => (
                <line key={`h${i}`} x1={g.from} y1={g.coord} x2={g.to} y2={g.coord} stroke="#ec4899" strokeWidth={1.4 / rfTransform[2]} strokeDasharray={`${5 / rfTransform[2]} ${4 / rfTransform[2]}`} />
              ))}
              {guides.gaps.map((g, i) => {
                const tick = 7 / rfTransform[2]
                const horiz = Math.abs(g.y2 - g.y1) < 0.01
                return (
                  <g key={`g${i}`} stroke="#f59e0b" strokeWidth={1.6 / rfTransform[2]}>
                    <line x1={g.x1} y1={g.y1} x2={g.x2} y2={g.y2} />
                    {horiz ? (
                      <>
                        <line x1={g.x1} y1={g.y1 - tick} x2={g.x1} y2={g.y1 + tick} />
                        <line x1={g.x2} y1={g.y2 - tick} x2={g.x2} y2={g.y2 + tick} />
                      </>
                    ) : (
                      <>
                        <line x1={g.x1 - tick} y1={g.y1} x2={g.x1 + tick} y2={g.y1} />
                        <line x1={g.x2 - tick} y1={g.y2} x2={g.x2 + tick} y2={g.y2} />
                      </>
                    )}
                  </g>
                )
              })}
            </svg>
          </ViewportPortal>
        )}
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => STATUS_HEX[statusKey((n.data as any)?.node?.status)] || STATUS_HEX.unknown}
          nodeStrokeWidth={2}
          maskColor={theme === 'dark' ? 'rgba(2,6,23,0.6)' : 'rgba(226,232,240,0.6)'}
          className="!bg-surface"
        />

        {/* NOC overlays (live mode): fleet summary bar + reading legend */}
        {liveMode && (
          <Panel position="top-center">
            <NocStatusBar detail={detail} nodesLive={nodesLive} liveData={liveData} updatedAt={liveUpdatedAt || Date.now()} />
          </Panel>
        )}
        {liveMode && (
          <Panel position="bottom-center">
            <MapLegend />
          </Panel>
        )}

        {/* Floating EVE-style toolbar */}
        {!liveMode && (
          <Panel position="top-left">
            <div className="flex items-center gap-1 rounded-lg border border-border bg-surface/90 p-1 shadow-lg backdrop-blur">
              <ToolBtn active={tool === 'select'} onClick={() => setTool('select')} title="Select / box-select (V) · hold Space to pan"><MousePointer2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={tool === 'connect'} onClick={() => setTool('connect')} title="Connect / draw cable (C)"><Cable className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={quickConnect} disabled={autoConnect.isPending} title="Quick connect — auto-draw CDP/LLDP links between placed devices"><Zap className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn onClick={undo} title="Undo (Ctrl+Z)"><Undo2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={redo} title="Redo (Ctrl+Shift+Z)"><Redo2 className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <InsertMenu onAdd={addShapeAt} />
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn active={smartOn} onClick={() => setSmartOn((v) => !v)} title="Smart guides — magnetic alignment & spacing while dragging"><Ruler className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={snapOn} onClick={() => setSnapOn((v) => !v)} title="Snap to grid"><Magnet className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={gridOn} onClick={() => setGridOn((v) => !v)} title="Show grid"><Grid3x3 className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={autoAlign} title="Auto-align all to grid"><Wand2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={tidyUp} title="Tidy layout — straighten rows/columns & even out spacing"><Sparkles className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('left')} title="Align left"><AlignStartVertical className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('top')} title="Align top"><AlignStartHorizontal className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('center-h')} title="Center horizontally"><AlignHorizontalJustifyCenter className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 3} onClick={() => applyAlign('distribute-h')} title="Distribute horizontally (equal spacing)"><AlignHorizontalDistributeCenter className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 3} onClick={() => applyAlign('distribute-v')} title="Distribute vertically (equal spacing)"><AlignVerticalDistributeCenter className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 1} danger onClick={deleteSelected} title="Delete selected"><Trash2 className="h-4 w-4" /></ToolBtn>
              {selCount > 0 && <span className="px-1.5 text-[11px] font-semibold text-muted">{selCount} sel</span>}
            </div>
          </Panel>
        )}

        {/* Annotation style inspector (single shape selected) */}
        {!liveMode && selectedShape && (
          <Panel position="top-right">
            <ShapeInspector
              shape={selectedShape}
              onChange={(patch, commit) => setShape(selectedShape.id, patch, commit)}
              onZ={(dir) => bumpShapeZ(selectedShape.id, dir)}
              onDelete={() => setPendingDelete({
                title: 'Delete annotation?',
                description: 'This removes the annotation from the map.',
                run: () => { deleteShape.mutate(selectedShape.id); selectedIds.current.delete(selectedShape.id); setNodes((nds) => nds.filter((n) => n.id !== selectedShape.id)); setSelectedShapeId(null) },
              })}
            />
          </Panel>
        )}
      </ReactFlow>

      {!liveMode && tool === 'select' && (
        <GroupResizer items={groupItems} transform={rfTransform} screenToFlow={screenToFlowPosition} onApply={applyGroup} />
      )}

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

      {editNode && (
        <NodeEditDialog
          node={editNode}
          saving={updateNode.isPending}
          onCancel={() => setEditNode(null)}
          onSave={(patch) => saveNode(editNode.id, patch)}
        />
      )}

      {editLink && (
        <LinkEditDialog
          link={editLink}
          source={nodeById.get(editLink.source_node_id)}
          target={nodeById.get(editLink.target_node_id)}
          saving={updateLink.isPending}
          onCancel={() => setEditLink(null)}
          onSave={(patch) => saveLink(editLink.id, patch)}
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
