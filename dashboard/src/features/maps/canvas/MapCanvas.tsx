import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
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
  reconnectEdge,
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
import { useIsMutating } from '@tanstack/react-query'
import {
  AlignEndHorizontal,
  AlignEndVertical,
  AlignHorizontalDistributeCenter,
  AlignHorizontalJustifyCenter,
  AlignStartHorizontal,
  AlignStartVertical,
  AlignVerticalDistributeCenter,
  AlignVerticalJustifyCenter,
  Cable,
  Check,
  ChevronDown,
  ClipboardPaste,
  CloudUpload,
  Copy,
  CopyPlus,
  Expand,
  ExternalLink,
  Grid3x3,
  Group,
  LayoutGrid,
  LocateFixed,
  Lock,
  LockOpen,
  Magnet,
  Maximize,
  MousePointer2,
  Pencil,
  Redo2,
  Ruler,
  Search,
  Shrink,
  Sparkles,
  Spline,
  SquareDashedMousePointer,
  Trash2,
  Undo2,
  Ungroup,
  Wand2,
  X,
  Zap,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { useTheme } from '@/stores/theme'
import {
  DISC,
  DISC_CX,
  DISC_CY,
  DISC_RADIUS,
  DEFAULT_ICON_FILL,
  LOGICAL_H,
  LOGICAL_W,
  discCenterToNodeXY,
  nodeXYToDiscCenter,
  pctToPx,
  pxToNodePct,
  pxToPct,
  linkWaypoints,
  endpointGeomFromNode,
  repositionEndpointWaypoints,
  pxToShape,
  reconcileLinkMetadataOnReconnect,
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
import { DeviceHoverCard, DeviceNode } from './DeviceNode'
import { ShapeNode } from './ShapeNode'
import { NetworkEdge } from './NetworkEdge'
import { ContextMenu, type ContextMenuState, type MenuItem } from './ContextMenu'
import { computeAlign, snap, type AlignOp } from './align'
import { MapModeContext } from './MapModeContext'
import { LinkDialog, type NewLink } from './LinkDialog'
import { DEVICE_DND_TYPE, SHAPE_DND_TYPE } from './DevicePalette'
import { LinkEditDialog, NodeEditDialog } from './EditDialogs'
import { IconPickerDialog, InsertMenu, SHAPE_PRESETS, ShapeInspector, iconSpec, type ShapeSpec } from './Annotations'
import { GroupResizer } from './GroupResizer'
import { MapLegend, NocStatusBar, ProblemsPanel, computeProblems, type Problem } from './NocOverlays'
import { NO_SNAP, computeSmartSnap, nodeToGuideBox, tidyLayout, type SmartSnapResult } from './smartGuides'

const nodeTypes = { device: DeviceNode, shape: ShapeNode }
const edgeTypes = { network: NetworkEdge }
const GRID = 40 // logical px
const NUDGE = 10 // arrow-key step (logical px); Shift ×4

/** Group membership lives in each item's metadata (`group_id`), so it persists
 *  with the map. Selecting any member selects the whole group. */
function groupIdOf(n: Node): string | null {
  const md = n.type === 'shape' ? (n.data as any)?.shape?.metadata : (n.data as any)?.node?.metadata
  return (md?.group_id as string) || null
}

function lockedOf(n: Node): boolean {
  const md = n.type === 'shape' ? (n.data as any)?.shape?.metadata : (n.data as any)?.node?.metadata
  return !!md?.locked
}

/** Find the device disc or annotation shape under a flow-space point — used to
 *  resolve where a detached cable end was dropped. Devices win (they render on
 *  top); among shapes the highest z-index wins. */
function hitTestEndpoint(nodes: Node[], fp: { x: number; y: number }): { id: string; kind: 'node' | 'shape' } | null {
  for (const n of nodes) {
    if (n.type !== 'device') continue
    const scale = ((n.data as any)?.node?.metadata?.size_scale as number) || 1
    const r = Math.max(DISC_RADIUS, (DISC * scale) / 2) + 8
    if (Math.hypot(fp.x - (n.position.x + DISC_CX), fp.y - (n.position.y + DISC_CY)) <= r) {
      return { id: n.id, kind: 'node' }
    }
  }
  const shapes = nodes.filter((n) => n.type === 'shape').sort((a, b) => (b.zIndex || 0) - (a.zIndex || 0))
  for (const n of shapes) {
    const w = n.measured?.width ?? (typeof n.width === 'number' ? n.width : 0)
    const h = n.measured?.height ?? (typeof n.height === 'number' ? n.height : 0)
    if (fp.x >= n.position.x - 4 && fp.x <= n.position.x + w + 4 && fp.y >= n.position.y - 4 && fp.y <= n.position.y + h + 4) {
      return { id: n.id, kind: 'shape' }
    }
  }
  return null
}

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
  showIfaceLabels?: boolean
  animate?: boolean
  showProblems?: boolean
  /** Lets the palette (outside the ReactFlow tree) insert a shape at the viewport centre. */
  insertRef?: React.MutableRefObject<((spec: ShapeSpec) => void) | null>
}

export function MapCanvas({ mapId, detail, liveData, nodesLive, liveUpdatedAt, liveMode, showThroughput, showIfaceLabels = true, animate = true, showProblems = true, insertRef }: MapCanvasProps) {
  const navigate = useNavigate()
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [snapOn, setSnapOn] = useState(false)
  const [gridOn, setGridOn] = useState(true)
  const [smartOn, setSmartOn] = useState(true) // magnetic alignment guides
  const [guides, setGuides] = useState<SmartSnapResult>(NO_SNAP)
  const [menu, setMenu] = useState<ContextMenuState>(null)
  const [selCount, setSelCount] = useState(0)
  const [selLocked, setSelLocked] = useState(false)
  const [tool, setTool] = useState<'select' | 'connect'>('select')
  const [panKey, setPanKey] = useState(false) // Space held → temporarily pan with left-drag
  // All deletions are explicit + confirmed. Nothing deletes without this.
  const [pendingDelete, setPendingDelete] = useState<{ title: string; description: string; run: () => void } | null>(null)
  const [pendingLink, setPendingLink] = useState<{ source: ManualMapNode; target: ManualMapNode } | null>(null)
  const [editNode, setEditNode] = useState<ManualMapNode | null>(null)
  const [editLink, setEditLink] = useState<{
    link: ManualMapLink
    annotation: boolean
    source?: ManualMapNode
    target?: ManualMapNode
    sourceLabel?: string
    targetLabel?: string
  } | null>(null)
  const [dropActive, setDropActive] = useState(false)
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null)
  const [annLinks, setAnnLinks] = useState<AnnotationLink[]>([])
  const annLinksRef = useRef<AnnotationLink[]>([])
  useEffect(() => { annLinksRef.current = annLinks }, [annLinks])
  // Right-click "Add here…" icon picker remembers where the click landed.
  const [iconPickAt, setIconPickAt] = useState<{ x: number; y: number } | null>(null)
  // Live mode: a clicked device stays pinned in a detail card.
  const [liveFocusId, setLiveFocusId] = useState<string | null>(null)

  const connectMode = tool === 'connect' && !liveMode
  const theme = useTheme((s) => s.theme)
  const { bulkMove, deleteNode, deleteLink, updateLink, updateNode, addLink, addNode, addShape, updateShape, deleteShape, saveMapMeta, autoConnect, invalidate } = useMapMutations(mapId)
  const { screenToFlowPosition, fitView, setCenter, getViewport, setViewport } = useReactFlow()
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
  // Lets the (stable) keyboard listener call the latest handlers.
  const deleteSelectedRef = useRef<() => void>(() => {})
  const duplicateSelectedRef = useRef<() => void>(() => {})
  const groupSelectedRef = useRef<() => void>(() => {})
  const ungroupSelectedRef = useRef<() => void>(() => {})
  const nudgeRef = useRef<(dx: number, dy: number) => void>(() => {})
  const copyRef = useRef<() => void>(() => {})
  const pasteRef = useRef<() => void>(() => {})
  const toggleLockRef = useRef<() => void>(() => {})
  const [hasGroupSel, setHasGroupSel] = useState(false)
  // Anything in flight? → the toolbar autosave chip shows "Saving…".
  const pendingMutations = useIsMutating()

  /* ── Undo / redo (reversible edits: moves, device/shape/link edits) ────── */
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
  useEffect(() => { undoStack.current = []; redoStack.current = [] }, [mapId])

  // Re-fit when switching maps or modes so the live wall always opens framed.
  useEffect(() => {
    const t = setTimeout(() => fitView({ padding: liveMode ? 0.12 : 0.2, duration: 350 }), 60)
    return () => clearTimeout(t)
  }, [mapId, liveMode, fitView])
  useEffect(() => { if (!liveMode) setLiveFocusId(null) }, [liveMode])

  /* ── Smart alignment guides ──────────────────────────────────────────── */
  const onNodesChangeSmart = useCallback((changes: NodeChange[]) => {
    if (!liveMode) {
      const extra: NodeChange[] = []
      for (const c of changes) {
        if (c.type !== 'select') continue
        const n = nodesRef.current.find((x) => x.id === c.id)
        const gid = n ? groupIdOf(n) : null
        if (!gid) continue
        for (const peer of nodesRef.current) {
          if (peer.id === c.id || groupIdOf(peer) !== gid) continue
          const already = changes.some((cc) => cc.type === 'select' && cc.id === peer.id)
            || extra.some((cc) => (cc as { id?: string }).id === peer.id)
          if (!already && peer.selected !== c.selected) extra.push({ type: 'select', id: peer.id, selected: c.selected })
        }
      }
      if (extra.length) changes = [...changes, ...extra]
    }
    if (!liveMode && smartOn && !snapOn) {
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
        setGuides(NO_SNAP)
      }
    }
    onNodesChange(changes)
  }, [liveMode, smartOn, snapOn, rfTransform, onNodesChange])

  /* ── Node metadata (movable label offset, lock, …) ─────────────────────── */
  const patchNodeMetaRef = useRef<(nodeId: string, patch: Record<string, unknown>, commit: boolean) => void>(() => {})
  patchNodeMetaRef.current = (nodeId, patch, commit) => {
    const prior = commit ? { ...(((nodesRef.current.find((n) => n.id === nodeId)?.data as any)?.node?.metadata) || {}) } : null
    setNodes((nds) => nds.map((n) => {
      if (n.id !== nodeId) return n
      const nd = (n.data as any).node
      const md = { ...(nd.metadata || {}), ...patch }
      return { ...n, draggable: !liveMode && !md.locked, data: { ...n.data, node: { ...nd, metadata: md } } }
    }))
    if (commit) {
      const next = { ...(prior || {}), ...patch }
      updateNode.mutate({ id: nodeId, patch: { metadata: next } }, { onError: () => toast.error('Failed to save device') })
      if (prior) pushHistory({ undo: () => applyNodeFields(nodeId, { metadata: prior }), redo: () => applyNodeFields(nodeId, { metadata: next }) })
    }
  }
  const setNodeLabelOffset = useCallback((nodeId: string, dx: number, dy: number, commit: boolean) => {
    patchNodeMetaRef.current(nodeId, { label_offset: { dx, dy } }, commit)
  }, [])

  /* ── Link shape / waypoint editing (ref-stable) ─────────────────────── */
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
    patchLinkMetaRef.current(linkId, shape === 'straight' ? { shape, waypoints: [] } : { shape }, true)
  }, [])

  const setIfacePos = useCallback((linkId: string, which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => {
    const cur = (edgesRef.current.find((e) => e.id === linkId)?.data as any)?.link?.metadata?.iface_pos || {}
    patchLinkMetaRef.current(linkId, { iface_pos: { ...cur, [which]: { ...(cur[which] || {}), ...pos } } }, commit)
  }, [])

  const resetIfaceLabels = useCallback((linkId: string) => {
    patchLinkMetaRef.current(linkId, { iface_pos: {} }, true)
  }, [])

  /* ── Annotation shape editing (ref-stable) ─────────────────────────────── */
  const patchShapeRef = useRef<(shapeId: string, patch: Record<string, unknown>, commit: boolean) => void>(() => {})
  patchShapeRef.current = (shapeId, patch, commit) => {
    const before = commit ? ((nodesRef.current.find((n) => n.id === shapeId && n.type === 'shape')?.data as any)?.shape as MapShape | undefined) : undefined
    setNodes((nds) => nds.map((n) => {
      if (n.id !== shapeId || n.type !== 'shape') return n
      const sh = (n.data as any).shape as MapShape
      const meta = patch.metadata ? { ...(sh.metadata || {}), ...(patch.metadata as object) } : sh.metadata
      const next = { ...sh, ...patch, metadata: meta }
      const geomChanged = ['x_pct', 'y_pct', 'w_pct', 'h_pct'].some((k) => k in patch)
      const r = geomChanged ? shapeToPx(next) : null
      return {
        ...n,
        draggable: !liveMode && !meta?.locked,
        data: { ...n.data, shape: next },
        ...(r ? { position: { x: r.x, y: r.y }, width: Math.round(r.w), height: Math.round(r.h), style: { width: Math.round(r.w), height: Math.round(r.h) } } : {}),
      }
    }))
    if (commit) {
      updateShape.mutate({ id: shapeId, patch }, { onError: () => toast.error('Failed to save annotation') })
      if (before) {
        const priorFields = { text: before.text ?? null, fill: before.fill ?? null, stroke: before.stroke ?? null, metadata: before.metadata || {}, x_pct: before.x_pct, y_pct: before.y_pct, w_pct: before.w_pct, h_pct: before.h_pct }
        const nextFields = { ...priorFields, ...patch, metadata: patch.metadata ? { ...(before.metadata || {}), ...(patch.metadata as object) } : (before.metadata || {}) }
        pushHistory({ undo: () => applyShapeFields(shapeId, priorFields), redo: () => applyShapeFields(shapeId, nextFields) })
      }
    }
  }
  const setShape = useCallback((shapeId: string, patch: Record<string, unknown>, commit: boolean) => {
    patchShapeRef.current(shapeId, patch, commit)
  }, [])

  useEffect(() => {
    const al = ((detail.metadata as any)?.annotation_links as AnnotationLink[]) || []
    setAnnLinks(al)
  }, [detail.metadata])

  const persistAnnLinks = useCallback((arr: AnnotationLink[]) => {
    saveMapMeta.mutate({ ...((detail.metadata as any) || {}), annotation_links: arr }, { onError: () => toast.error('Failed to save link') })
  }, [saveMapMeta, detail.metadata])

  const annArrayFromEdges = useCallback((overrideId?: string, overrideMeta?: Record<string, unknown>, overrideFields?: { label?: string | null; link_type?: string }): AnnotationLink[] => {
    return edgesRef.current.filter((e) => (e.data as any)?.annotation).map((e) => {
      const dd = e.data as any
      const isOv = e.id === overrideId
      return {
        id: e.id, source: e.source, target: e.target,
        source_type: dd.sourceType, target_type: dd.targetType,
        label: isOv && overrideFields ? (overrideFields.label ?? null) : (dd.link?.label ?? null),
        link_type: (isOv && overrideFields?.link_type) || dd.link?.link_type || 'manual',
        metadata: isOv && overrideMeta ? (overrideMeta as any) : (dd.link?.metadata || {}),
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
        draggable: !liveMode && !n.metadata?.locked,
        selected: selectedIds.current.has(n.id),
        style: liveMode ? { pointerEvents: 'all' as const } : undefined,
      }
    })
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
        draggable: !liveMode && !s.metadata?.locked,
        selected: selectedIds.current.has(s.id),
        zIndex: s.z_index || 0,
      }
    })
    setNodes([...shapeNodes, ...deviceNodes])
  }, [detail.nodes, detail.shapes, liveMode, nodesLive, setNodes, setNodeLabelOffset, setShape])

  useEffect(() => {
    const visible = detail.links.filter((l) => nodeById.has(l.source_node_id) && nodeById.has(l.target_node_id))

    const PARALLEL_GAP = 18
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
          sourceStatus: (liveMode && nodesLive[l.source_node_id]?.status) || s.status,
          targetStatus: (liveMode && nodesLive[l.target_node_id]?.status) || t.status,
          live: liveData[l.id],
          liveMode,
          showThroughput,
          showIfaceLabels,
          animate,
          parallelOffset: offsetById.get(l.id) || 0,
          setWaypoints: (wpsPx: { x: number; y: number }[], commit: boolean) => setEdgeWaypoints(l.id, wpsPx, commit),
          setIfacePos: (which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => setIfacePos(l.id, which, pos, commit),
          reconnectEnd: (which: 'src' | 'dst', pt: { x: number; y: number }) => reconnectEndRef.current(l.id, which, pt),
        },
      }
    })

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
          live: liveData[al.id],
          liveMode,
          showThroughput,
          showIfaceLabels,
          animate,
          annotation: true,
          sourceType: al.source_type,
          targetType: al.target_type,
          parallelOffset: 0,
          setWaypoints: (wpsPx: { x: number; y: number }[], commit: boolean) => setEdgeWaypoints(al.id, wpsPx, commit),
          setIfacePos: (which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => setIfacePos(al.id, which, pos, commit),
          reconnectEnd: (which: 'src' | 'dst', pt: { x: number; y: number }) => reconnectEndRef.current(al.id, which, pt),
        },
      }))

    setEdges([...deviceEdges, ...annEdges])
  }, [detail.links, detail.shapes, annLinks, liveData, liveMode, nodesLive, showThroughput, showIfaceLabels, animate, nodeById, setEdges, setEdgeWaypoints, setIfacePos])

  /* ── Persistence ─────────────────────────────────────────────── */
  const persistPositions = useCallback((items: { id: string; x: number; y: number }[]) => {
    const payload = items.map((it) => {
      const c = nodeXYToDiscCenter(it.x, it.y)
      const p = pxToNodePct(c.x, c.y)
      return { id: it.id, x_pct: p.x_pct, y_pct: p.y_pct }
    })
    bulkMove.mutate(payload, { onError: () => toast.error('Failed to save layout') })
  }, [bulkMove])

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

  const shiftConnectedWaypoints = useCallback((deltas: Map<string, { dx: number; dy: number }>) => {
    const updates: { id: string; meta: Record<string, unknown> }[] = []
    edgesRef.current.forEach((e) => {
      if ((e.data as any).annotation) return
      const ds = deltas.get(e.source)
      if (!ds || !deltas.get(e.target)) return
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

  const applyNodeFields = useCallback((id: string, fields: Record<string, unknown>) => {
    setNodes((nds) => nds.map((n) => {
      if (n.id !== id) return n
      const node = { ...(n.data as any).node, ...fields }
      return { ...n, draggable: !liveMode && !node.metadata?.locked, data: { ...n.data, node } }
    }))
    updateNode.mutate({ id, patch: fields }, {
      onSuccess: () => { if (fields.device_id) invalidate() },
      onError: () => toast.error('Failed to save device'),
    })
  }, [setNodes, updateNode, invalidate, liveMode])

  const applyShapeFields = useCallback((id: string, fields: Record<string, unknown>) => {
    setNodes((nds) => nds.map((n) => {
      if (n.id !== id || n.type !== 'shape') return n
      const sh = { ...(n.data as any).shape, ...fields }
      const r = shapeToPx(sh)
      return { ...n, draggable: !liveMode && !sh.metadata?.locked, data: { ...n.data, shape: sh }, position: { x: r.x, y: r.y }, width: Math.round(r.w), height: Math.round(r.h), style: { width: Math.round(r.w), height: Math.round(r.h) } }
    }))
    updateShape.mutate({ id, patch: fields }, { onError: () => toast.error('Failed to save annotation') })
  }, [setNodes, updateShape, liveMode])

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

  /* ── Connect (drag between handles, including the hover plug) ─────────── */
  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return
    const s = nodeById.get(c.source)
    const t = nodeById.get(c.target)
    if (s && t) { setPendingLink({ source: s, target: t }); return }
    const al: AnnotationLink = {
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
        toast.error(e?.response?.status === 409 ? 'A link between these two devices already exists.' : 'Failed to create link')
      },
    })
  }, [addLink])

  const endpointLabel = useCallback((nodeId: string, kind: 'node' | 'shape') => {
    if (kind === 'node') {
      const n = nodeById.get(nodeId)
      return n?.label || n?.hostname || 'Device'
    }
    const sh = nodesRef.current.find((n) => n.id === nodeId && n.type === 'shape')
    const shape = (sh?.data as any)?.shape
    return shape?.text || shape?.kind || 'Annotation'
  }, [nodeById])

  const onReconnect = useCallback((oldEdge: Edge, connection: Connection) => {
    if (liveMode || !connection.source || !connection.target || connection.source === connection.target) return
    const dd = oldEdge.data as any
    const link = dd?.link as ManualMapLink | undefined
    if (!link) return

    const oldSrc = oldEdge.source
    const oldTgt = oldEdge.target
    const newSrc = connection.source
    const newTgt = connection.target
    const srcType: 'node' | 'shape' = nodeById.has(newSrc) ? 'node' : 'shape'
    const tgtType: 'node' | 'shape' = nodeById.has(newTgt) ? 'node' : 'shape'
    const meta = reconcileLinkMetadataOnReconnect((link.metadata || {}) as any, oldSrc, oldTgt, newSrc, newTgt, srcType, tgtType)
    const endpointsChanged = oldSrc !== newSrc || oldTgt !== newTgt

    setEdges((eds) => reconnectEdge(oldEdge, connection, eds).map((e) => {
      if (e.id !== oldEdge.id) return e
      const patched = { ...link, source_node_id: e.source, target_node_id: e.target, metadata: meta }
      return {
        ...e,
        data: {
          ...e.data,
          link: patched,
          sourceStatus: srcType === 'node' ? (nodeById.get(e.source)?.status || 'unknown') : 'up',
          targetStatus: tgtType === 'node' ? (nodeById.get(e.target)?.status || 'unknown') : 'up',
          ...(dd.annotation ? { sourceType: srcType, targetType: tgtType } : {}),
        },
      }
    }))

    if (dd.annotation) {
      const next = annLinksRef.current.map((al) => (al.id !== oldEdge.id ? al : { ...al, source: newSrc, target: newTgt, source_type: srcType, target_type: tgtType, metadata: meta as any }))
      setAnnLinks(next)
      persistAnnLinks(next)
    } else {
      updateLink.mutate({ id: oldEdge.id, patch: { source_node_id: newSrc, target_node_id: newTgt, metadata: meta } }, { onError: () => toast.error('Failed to reconnect link') })
    }

    if (endpointsChanged && (srcType === 'node' || tgtType === 'node')) {
      setEditLink({
        link: { ...link, source_node_id: newSrc, target_node_id: newTgt, metadata: meta },
        annotation: !!dd.annotation,
        source: srcType === 'node' ? nodeById.get(newSrc) : undefined,
        target: tgtType === 'node' ? nodeById.get(newTgt) : undefined,
        sourceLabel: srcType === 'shape' ? endpointLabel(newSrc, 'shape') : undefined,
        targetLabel: tgtType === 'shape' ? endpointLabel(newTgt, 'shape') : undefined,
      })
      toast.info('Select interface label(s) for the new endpoint(s)')
    } else if (endpointsChanged) {
      toast.success('Link reconnected')
    }
  }, [liveMode, nodeById, setEdges, persistAnnLinks, updateLink, endpointLabel])

  const reconnectEndRef = useRef<(linkId: string, which: 'src' | 'dst', fp: { x: number; y: number }) => void>(() => {})
  reconnectEndRef.current = (linkId, which, fp) => {
    const edge = edgesRef.current.find((e) => e.id === linkId)
    if (!edge) return
    const hit = hitTestEndpoint(nodesRef.current, fp)
    if (!hit) { toast.info('Drop the cable end on a device or shape to reconnect') ; return }
    const curEnd = which === 'src' ? edge.source : edge.target
    const otherEnd = which === 'src' ? edge.target : edge.source
    if (hit.id === curEnd) {
      const link = (edge.data as any)?.link as ManualMapLink | undefined
      if (!link) return
      const node = nodesRef.current.find((n) => n.id === hit.id)
      if (!node) return
      const geom = endpointGeomFromNode(node.position, node.type, node.data, node.measured, node.width, node.height)
      if (!geom) return
      const otherNode = nodesRef.current.find((n) => n.id === otherEnd)
      const otherGeom = otherNode ? endpointGeomFromNode(otherNode.position, otherNode.type, otherNode.data, otherNode.measured, otherNode.width, otherNode.height) : null
      const stored = linkWaypoints(link).map((w) => pctToPx(w))
      const newWps = repositionEndpointWaypoints(stored, which, fp, geom, otherGeom?.center ?? fp)
      setEdgeWaypoints(link.id, newWps, true)
      return
    }
    if (hit.id === otherEnd) { toast.error('Both cable ends cannot attach to the same item'); return }
    if (!(edge.data as any)?.annotation && hit.kind === 'shape') {
      const link = (edge.data as any).link as ManualMapLink
      const newSrc = which === 'src' ? hit.id : edge.source
      const newTgt = which === 'dst' ? hit.id : edge.target
      const srcType: 'node' | 'shape' = nodeById.has(newSrc) ? 'node' : 'shape'
      const tgtType: 'node' | 'shape' = nodeById.has(newTgt) ? 'node' : 'shape'
      const meta = reconcileLinkMetadataOnReconnect((link.metadata || {}) as any, edge.source, edge.target, newSrc, newTgt, srcType, tgtType)
      const al: AnnotationLink = {
        id: newAnnId(), source: newSrc, target: newTgt, source_type: srcType, target_type: tgtType,
        label: link.label ?? null, link_type: link.link_type || 'manual', metadata: meta as any,
      }
      const next = [...annLinksRef.current, al]
      setAnnLinks(next)
      persistAnnLinks(next)
      deleteLink.mutate(edge.id, { onError: () => toast.error('Failed to move the link') })
      setEdges((eds) => eds.filter((e) => e.id !== edge.id))
      if (srcType === 'node' || tgtType === 'node') {
        setEditLink({
          link: { ...link, id: al.id, source_node_id: newSrc, target_node_id: newTgt, metadata: meta },
          annotation: true,
          source: srcType === 'node' ? nodeById.get(newSrc) : undefined,
          target: tgtType === 'node' ? nodeById.get(newTgt) : undefined,
          sourceLabel: srcType === 'shape' ? endpointLabel(newSrc, 'shape') : undefined,
          targetLabel: tgtType === 'shape' ? endpointLabel(newTgt, 'shape') : undefined,
        })
        toast.info('Select interface label(s) for the new endpoint(s)')
      } else {
        toast.success('Link reconnected')
      }
      return
    }
    onReconnect(edge, { source: which === 'src' ? hit.id : edge.source, target: which === 'dst' ? hit.id : edge.target, sourceHandle: 'c', targetHandle: 'c' })
  }

  /* ── Duplicate / copy / paste ──────────────────────────────────────────── */
  const newAnnId = () => `al-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  const DUP_OFF = { x: 1.2, y: 2 }

  const duplicateShapeAsync = useCallback(async (shapeId: string, off = DUP_OFF): Promise<string | null> => {
    const n = nodesRef.current.find((x) => x.id === shapeId && x.type === 'shape')
    const sh = (n?.data as any)?.shape as MapShape | undefined
    if (!n || !sh) return null
    const r = shapeToPx(sh)
    const w = typeof n.width === 'number' ? n.width : r.w
    const h = typeof n.height === 'number' ? n.height : r.h
    const p = pxToShape(n.position.x, n.position.y, w, h)
    try {
      const created = await addShape.mutateAsync({
        kind: sh.kind, x_pct: Math.min(96, p.x_pct + off.x), y_pct: Math.min(96, p.y_pct + off.y),
        w_pct: p.w_pct, h_pct: p.h_pct, text: sh.text ?? null, fill: sh.fill ?? null, stroke: sh.stroke ?? null,
        z_index: sh.z_index || 0, metadata: { ...(sh.metadata || {}), locked: false, group_id: null },
      }) as { id: string }
      return created.id
    } catch {
      toast.error('Failed to duplicate annotation')
      return null
    }
  }, [addShape])

  const duplicateShape = useCallback((shapeId: string, silent = false) => {
    void duplicateShapeAsync(shapeId).then((id) => { if (id && !silent) toast.success('Duplicated') })
  }, [duplicateShapeAsync])

  const duplicateDevice = useCallback(async (nodeId: string, off = DUP_OFF): Promise<string | null> => {
    const n = nodesRef.current.find((x) => x.id === nodeId && x.type === 'device')
    const dev = (n?.data as any)?.node as ManualMapNode | undefined
    if (!n || !dev) return null
    const c = nodeXYToDiscCenter(n.position.x, n.position.y)
    const p = pxToPct(c.x, c.y)
    try {
      const created = await addNode.mutateAsync({
        device_id: dev.device_id,
        x_pct: Math.max(2, Math.min(96, p.x_pct + off.x)),
        y_pct: Math.max(2, Math.min(96, p.y_pct + off.y)),
        icon: dev.icon || 'auto',
        label: dev.label && dev.label !== dev.hostname ? dev.label : null,
        metadata: { ...(dev.metadata || {}), locked: false, group_id: null },
      })
      return created.id
    } catch (e: any) {
      toast.error(e?.response?.status === 409 ? 'This device is already on the map — apply DB migration 031 to allow duplicates' : 'Failed to duplicate device')
      return null
    }
  }, [addNode])

  const duplicateEdge = useCallback((edge: Edge) => {
    const dd = edge.data as any
    const link = dd?.link
    if (!link) return
    if (dd.annotation) {
      const src = annLinksRef.current.find((a) => a.id === edge.id)
      if (!src) return
      const next = [...annLinksRef.current, { ...src, id: newAnnId() }]
      setAnnLinks(next)
      persistAnnLinks(next)
      toast.success('Connection duplicated')
      return
    }
    addLink.mutate({
      source_node_id: link.source_node_id, target_node_id: link.target_node_id,
      label: link.label ?? null, link_type: link.link_type || 'manual', metadata: { ...(link.metadata || {}) },
    }, { onSuccess: () => toast.success('Link duplicated'), onError: () => toast.error('Failed to duplicate link') })
  }, [addLink, persistAnnLinks])

  /** Copy a set of items (and the cables between them) with an offset. */
  const duplicateItems = useCallback(async (ids: string[], off = DUP_OFF, silent = false) => {
    const sel = nodesRef.current.filter((n) => ids.includes(n.id))
    if (!sel.length) { if (!silent) toast.info('Nothing selected to duplicate'); return }
    const idMap = new Map<string, string>()
    const kindOf = new Map<string, 'node' | 'shape'>()
    for (const n of sel) {
      const newId = n.type === 'shape' ? await duplicateShapeAsync(n.id, off) : await duplicateDevice(n.id, off)
      if (newId) { idMap.set(n.id, newId); kindOf.set(newId, n.type === 'shape' ? 'shape' : 'node') }
    }
    if (!idMap.size) return
    const shiftMeta = (meta: any): Record<string, unknown> => {
      const wps = meta?.waypoints
      if (!Array.isArray(wps) || !wps.length) return { ...(meta || {}) }
      return { ...meta, waypoints: wps.map((w: any) => ({ x_pct: Math.min(100, w.x_pct + off.x), y_pct: Math.min(100, w.y_pct + off.y) })) }
    }
    let cables = 0
    const newAnns: AnnotationLink[] = []
    for (const e of edgesRef.current) {
      const s = idMap.get(e.source)
      const t = idMap.get(e.target)
      if (!s || !t) continue
      const dd = e.data as any
      const link = dd?.link
      const meta = shiftMeta(link?.metadata)
      if (dd?.annotation) {
        newAnns.push({ id: newAnnId(), source: s, target: t, source_type: kindOf.get(s)!, target_type: kindOf.get(t)!, label: link?.label ?? null, link_type: link?.link_type || 'manual', metadata: meta as any })
        cables++
      } else {
        try {
          await addLink.mutateAsync({ source_node_id: s, target_node_id: t, label: link?.label ?? null, link_type: link?.link_type || 'manual', metadata: meta })
          cables++
        } catch { /* skip a failed cable, keep going */ }
      }
    }
    if (newAnns.length) {
      const next = [...annLinksRef.current, ...newAnns]
      setAnnLinks(next)
      persistAnnLinks(next)
    }
    // Select the copies so they can be moved straight away.
    selectedIds.current = new Set(idMap.values())
    if (!silent) toast.success(`Duplicated ${idMap.size} item${idMap.size > 1 ? 's' : ''}${cables ? ` and ${cables} cable${cables > 1 ? 's' : ''}` : ''}`)
  }, [duplicateShapeAsync, duplicateDevice, addLink, persistAnnLinks])

  const duplicateSelected = useCallback(() => duplicateItems(nodesRef.current.filter((n) => n.selected).map((n) => n.id)), [duplicateItems])
  duplicateSelectedRef.current = duplicateSelected

  // Clipboard = ids of the copied items (they still live on this map).
  const clipboardRef = useRef<string[]>([])
  const pasteCount = useRef(0)
  const copySelected = useCallback(() => {
    const ids = nodesRef.current.filter((n) => n.selected).map((n) => n.id)
    if (!ids.length) { toast.info('Select something to copy'); return }
    clipboardRef.current = ids
    pasteCount.current = 0
    toast.success(`Copied ${ids.length} item${ids.length > 1 ? 's' : ''}`)
  }, [])
  const paste = useCallback(() => {
    const ids = clipboardRef.current.filter((id) => nodesRef.current.some((n) => n.id === id))
    if (!ids.length) { toast.info('Clipboard is empty'); return }
    pasteCount.current += 1
    void duplicateItems(ids, { x: DUP_OFF.x * pasteCount.current, y: DUP_OFF.y * pasteCount.current })
  }, [duplicateItems])
  copyRef.current = copySelected
  pasteRef.current = paste

  /* ── Group / ungroup ────────────────────────────────────────────────────── */
  const groupSelected = useCallback(() => {
    const sel = nodesRef.current.filter((n) => n.selected)
    if (sel.length < 2) { toast.info('Select at least two items to group'); return }
    const gid = `grp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`
    sel.forEach((n) => {
      if (n.type === 'shape') patchShapeRef.current(n.id, { metadata: { ...(((n.data as any).shape?.metadata) || {}), group_id: gid } }, true)
      else patchNodeMetaRef.current(n.id, { group_id: gid }, true)
    })
    toast.success(`Grouped ${sel.length} items — they now move and duplicate together`)
  }, [])

  const ungroupSelected = useCallback(() => {
    const sel = nodesRef.current.filter((n) => n.selected && groupIdOf(n))
    if (!sel.length) { toast.info('Selection has no group'); return }
    sel.forEach((n) => {
      if (n.type === 'shape') patchShapeRef.current(n.id, { metadata: { ...(((n.data as any).shape?.metadata) || {}), group_id: null } }, true)
      else patchNodeMetaRef.current(n.id, { group_id: null }, true)
    })
    toast.success('Ungrouped')
  }, [])
  groupSelectedRef.current = groupSelected
  ungroupSelectedRef.current = ungroupSelected

  /* ── Lock / unlock ──────────────────────────────────────────────────────── */
  const setLockedFor = useCallback((ids: string[], locked: boolean) => {
    const items = nodesRef.current.filter((n) => ids.includes(n.id))
    items.forEach((n) => {
      if (n.type === 'shape') patchShapeRef.current(n.id, { metadata: { ...(((n.data as any).shape?.metadata) || {}), locked } }, true)
      else patchNodeMetaRef.current(n.id, { locked }, true)
    })
    setSelLocked(locked)
    if (items.length) toast.success(locked ? `Locked ${items.length} item${items.length > 1 ? 's' : ''}` : 'Unlocked')
  }, [])
  const toggleLockSelected = useCallback(() => {
    const sel = nodesRef.current.filter((n) => n.selected)
    if (!sel.length) { toast.info('Select something to lock'); return }
    const allLocked = sel.every(lockedOf)
    setLockedFor(sel.map((n) => n.id), !allLocked)
  }, [setLockedFor])
  toggleLockRef.current = toggleLockSelected

  /* ── Quick resize ───────────────────────────────────────────────────────── */
  const resizeSelected = useCallback((factor: number) => {
    const sel = nodesRef.current.filter((n) => n.selected && !lockedOf(n))
    if (!sel.length) { toast.info('Nothing (unlocked) selected to resize'); return }
    sel.forEach((n) => {
      if (n.type === 'shape') {
        const sh = (n.data as any).shape as MapShape
        const w = Math.max(1, Math.min(100, sh.w_pct * factor))
        const h = Math.max(1, Math.min(100, sh.h_pct * factor))
        patchShapeRef.current(n.id, {
          w_pct: w, h_pct: h,
          x_pct: Math.max(0, Math.min(100, sh.x_pct - (w - sh.w_pct) / 2)),
          y_pct: Math.max(0, Math.min(100, sh.y_pct - (h - sh.h_pct) / 2)),
        }, true)
      } else {
        const cur = ((n.data as any).node?.metadata?.size_scale as number) || 1
        patchNodeMetaRef.current(n.id, { size_scale: Math.max(0.4, Math.min(3, cur * factor)) }, true)
      }
    })
  }, [])

  const resizeIconFill = useCallback((factor: number) => {
    const sel = nodesRef.current.filter((n) => n.selected && n.type !== 'shape')
    if (!sel.length) { toast.info('Select device nodes to adjust icon fill'); return }
    sel.forEach((n) => {
      const md = ((n.data as any).node?.metadata || {}) as Record<string, unknown>
      const cur = typeof md.icon_fill === 'number' ? md.icon_fill : DEFAULT_ICON_FILL
      patchNodeMetaRef.current(n.id, { icon_fill: Math.max(0.45, Math.min(0.95, +(cur * factor).toFixed(2))) }, true)
    })
  }, [])

  const resizeLink = useCallback((linkId: string, factor: number) => {
    const cur = Number(((edgesRef.current.find((e) => e.id === linkId)?.data as any)?.link?.metadata?.width_scale)) || 1
    patchLinkMetaRef.current(linkId, { width_scale: Math.max(0.4, Math.min(4, +(cur * factor).toFixed(2))) }, true)
  }, [])

  /* ── Drop from the palette (device or shape) ───────────────────────────── */
  const onCanvasDragOver = useCallback((e: React.DragEvent) => {
    if (liveMode) return
    const t = e.dataTransfer.types
    if (!t.includes(DEVICE_DND_TYPE) && !t.includes(SHAPE_DND_TYPE)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    if (!dropActive) setDropActive(true)
  }, [liveMode, dropActive])

  const addShapeAtFlow = useCallback((spec: ShapeSpec, c: { x: number; y: number }) => {
    const w_pct = spec.w_pct ?? 12, h_pct = spec.h_pct ?? 8
    const wPx = (w_pct / 100) * LOGICAL_W, hPx = (h_pct / 100) * LOGICAL_H
    const pct = pxToShape(c.x - wPx / 2, c.y - hPx / 2, wPx, hPx)
    addShape.mutate({
      kind: spec.kind, x_pct: pct.x_pct, y_pct: pct.y_pct, w_pct, h_pct,
      text: spec.text ?? null, fill: spec.fill ?? null, stroke: spec.stroke ?? null,
      metadata: spec.metadata ?? {},
    }, { onSuccess: () => toast.success('Added'), onError: () => toast.error('Failed to add annotation') })
  }, [addShape])

  const onCanvasDrop = useCallback((e: React.DragEvent) => {
    setDropActive(false)
    if (liveMode) return
    const shapeJson = e.dataTransfer.getData(SHAPE_DND_TYPE)
    if (shapeJson) {
      e.preventDefault()
      try {
        const spec = JSON.parse(shapeJson) as ShapeSpec
        addShapeAtFlow(spec, screenToFlowPosition({ x: e.clientX, y: e.clientY }))
      } catch { toast.error('Could not place that shape') }
      return
    }
    const deviceId = e.dataTransfer.getData(DEVICE_DND_TYPE)
    if (!deviceId) return
    e.preventDefault()
    const center = screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const p = pxToNodePct(center.x, center.y)
    const already = detail.nodes.some((n) => n.device_id === deviceId)
    addNode.mutate(
      { device_id: deviceId, x_pct: p.x_pct, y_pct: p.y_pct },
      {
        onSuccess: () => toast.success(already ? 'Second tile placed for this device' : 'Device placed'),
        onError: (err: any) => toast.error(err?.response?.status === 409 ? 'This device is already on the map (DB migration 031 allows duplicates)' : 'Failed to place device'),
      },
    )
  }, [liveMode, detail.nodes, screenToFlowPosition, addNode, addShapeAtFlow])

  /* ── Edit dialogs ──────────────────────────────────────────────────────── */
  const saveNode = useCallback((id: string, patch: { label: string | null; icon: string; metadata?: Record<string, unknown>; device_id?: string }) => {
    const before = (nodesRef.current.find((n) => n.id === id)?.data as any)?.node
    if (before) {
      const prior = { label: before.label ?? null, icon: before.icon, metadata: before.metadata || {}, ...(patch.device_id ? { device_id: before.device_id } : {}) }
      pushHistory({ undo: () => applyNodeFields(id, prior), redo: () => applyNodeFields(id, patch) })
    }
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, node: { ...(n.data as any).node, ...patch } } } : n)))
    updateNode.mutate({ id, patch }, {
      onSuccess: () => {
        setEditNode(null)
        if (patch.device_id) { invalidate(); toast.success('Device profile changed — edit its links to re-map interfaces') }
        else toast.success('Device updated')
      },
      onError: (e: any) => toast.error(e?.response?.status === 409 ? 'That device is already on this map — apply DB migration 031 to allow duplicates' : 'Failed to save device'),
    })
  }, [setNodes, updateNode, invalidate, applyNodeFields, pushHistory])

  const saveLink = useCallback((id: string, patch: { label: string | null; link_type: string; metadata: Record<string, unknown> }, annotation: boolean) => {
    setEdges((eds) => eds.map((e) => (e.id === id ? { ...e, data: { ...e.data, link: { ...(e.data as any).link, ...patch } } } : e)))
    if (annotation) {
      persistAnnLinks(annArrayFromEdges(id, patch.metadata, { label: patch.label, link_type: patch.link_type }))
      setAnnLinks((arr) => arr.map((a) => (a.id === id ? { ...a, label: patch.label, link_type: patch.link_type, metadata: patch.metadata as any } : a)))
      setEditLink(null)
      toast.success('Link updated')
      return
    }
    updateLink.mutate({ id, patch }, {
      onSuccess: () => { setEditLink(null); toast.success('Link updated') },
      onError: () => toast.error('Failed to save link'),
    })
  }, [setEdges, updateLink, persistAnnLinks, annArrayFromEdges])

  const openEditLink = useCallback((edge: Edge) => {
    const dd = edge.data as any
    const link = dd.link
    const isAnnotation = !!dd.annotation
    const srcId = link.source_node_id, tgtId = link.target_node_id
    const srcType = isAnnotation ? (dd.sourceType || (nodeById.has(srcId) ? 'node' : 'shape')) : 'node'
    const tgtType = isAnnotation ? (dd.targetType || (nodeById.has(tgtId) ? 'node' : 'shape')) : 'node'
    setEditLink({
      link, annotation: isAnnotation,
      source: srcType === 'node' ? nodeById.get(srcId) : undefined,
      target: tgtType === 'node' ? nodeById.get(tgtId) : undefined,
      sourceLabel: srcType === 'shape' ? endpointLabel(srcId, 'shape') : undefined,
      targetLabel: tgtType === 'shape' ? endpointLabel(tgtId, 'shape') : undefined,
    })
  }, [nodeById, endpointLabel])

  /* ── Drag bookkeeping ───────────────────────────────────────────────────── */
  const dragPrevRef = useRef<{ id: string; x: number; y: number }[]>([])
  const onNodeDragStart = useCallback((_e: unknown, node: Node, dragged: Node[]) => {
    const moving = dragged && dragged.length ? dragged : [node]
    dragPrevRef.current = moving.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
  }, [])
  const onNodeDragStop = useCallback((_e: unknown, node: Node, dragged: Node[]) => {
    setGuides(NO_SNAP)
    const moved = (dragged && dragged.length ? dragged : [node]).map((n) => nodesRef.current.find((x) => x.id === n.id) || n)
    const next = moved.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    applyPositions(next)
    const prev = dragPrevRef.current
    const deltas = new Map<string, { dx: number; dy: number }>()
    next.forEach((nx) => { const p = prev.find((q) => q.id === nx.id); if (p) deltas.set(nx.id, { dx: nx.x - p.x, dy: nx.y - p.y }) })
    shiftConnectedWaypoints(deltas)
    if (prev.length && prev.some((p, i) => p.x !== next[i]?.x || p.y !== next[i]?.y)) {
      pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
    }
  }, [applyPositions, shiftConnectedWaypoints, pushHistory])

  // Arrow-key nudge (Shift = ×4). Locked items stay put.
  const nudge = useCallback((dx: number, dy: number) => {
    const sel = nodesRef.current.filter((n) => n.selected && !lockedOf(n))
    if (!sel.length) return
    const prev = sel.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const next = sel.map((n) => ({ id: n.id, x: n.position.x + dx, y: n.position.y + dy }))
    applyPositions(next)
    const deltas = new Map(sel.map((n) => [n.id, { dx, dy }]))
    shiftConnectedWaypoints(deltas)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
  }, [applyPositions, shiftConnectedWaypoints, pushHistory])
  nudgeRef.current = nudge

  const quickConnect = useCallback(() => {
    if (autoConnect.isPending) return
    autoConnect.mutate(undefined, {
      onSuccess: (r) => toast.success(r.created > 0 ? `Connected ${r.created} link${r.created > 1 ? 's' : ''} from CDP/LLDP` : 'No new discovered links found'),
      onError: () => toast.error('Quick connect failed'),
    })
  }, [autoConnect])

  const addShapeAt = useCallback((spec: ShapeSpec) => {
    const rect = wrapperRef.current?.getBoundingClientRect()
    const cx = rect ? rect.left + rect.width / 2 : 400
    const cy = rect ? rect.top + rect.height / 2 : 300
    addShapeAtFlow(spec, screenToFlowPosition({ x: cx, y: cy }))
  }, [screenToFlowPosition, addShapeAtFlow])
  useEffect(() => {
    if (!insertRef) return
    insertRef.current = addShapeAt
    return () => { insertRef.current = null }
  }, [insertRef, addShapeAt])

  /* ── Alignment / auto-align (shapes included, locked items skipped) ───── */
  const applyAlign = useCallback((op: AlignOp) => {
    const sel = nodes.filter((n) => n.selected && !lockedOf(n))
    if (sel.length < 2) return
    const prev = sel.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const moves = computeAlign(sel, op)
    const next = sel.map((n) => ({ id: n.id, ...(moves.get(n.id) || n.position) }))
    applyPositions(next)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
  }, [nodes, applyPositions, pushHistory])

  const autoAlign = useCallback(() => {
    const all = nodes.filter((n) => n.type !== 'shape' && !lockedOf(n))
    if (!all.length) return
    const prev = all.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }))
    const next = all.map((n) => ({ id: n.id, x: snap(n.position.x, GRID), y: snap(n.position.y, GRID) }))
    applyPositions(next)
    pushHistory({ undo: () => applyPositions(prev), redo: () => applyPositions(next) })
    toast.success('Aligned all to grid')
  }, [nodes, applyPositions, pushHistory])

  const tidyUp = useCallback(() => {
    const all = tidyLayout(nodesRef.current)
    const moves = new Map([...all].filter(([id]) => { const n = nodesRef.current.find((x) => x.id === id); return n && !lockedOf(n) }))
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

  const matchSizes = useCallback((refNode: ManualMapNode) => {
    const scale = refNode.metadata?.size_scale || 1
    const ids = nodesRef.current.filter((n) => n.selected && n.type === 'device' && n.id !== refNode.id).map((n) => n.id)
    ids.forEach((id) => patchNodeMetaRef.current(id, { size_scale: scale }, true))
    if (ids.length) toast.success(`Matched ${ids.length} device size${ids.length > 1 ? 's' : ''}`)
  }, [])

  const resetLabels = useCallback(() => {
    const ids = nodesRef.current.filter((n) => n.selected && n.type === 'device').map((n) => n.id)
    ids.forEach((id) => patchNodeMetaRef.current(id, { label_offset: { dx: 0, dy: 0 } }, true))
    if (ids.length) toast.success('Label positions reset')
  }, [])

  /* ── Locate / focus ─────────────────────────────────────────────────────── */
  const focusNode = useCallback((id: string, select = !liveMode) => {
    const n = nodesRef.current.find((x) => x.id === id)
    if (!n) return
    fitView({ nodes: [{ id }], duration: 450, padding: 2.2, maxZoom: 1.3 })
    if (select) setNodes((nds) => nds.map((x) => ({ ...x, selected: x.id === id })))
  }, [fitView, setNodes, liveMode])

  const focusEdge = useCallback((id: string) => {
    const e = edgesRef.current.find((x) => x.id === id)
    if (!e) return
    const a = nodesRef.current.find((x) => x.id === e.source)
    const b = nodesRef.current.find((x) => x.id === e.target)
    if (!a || !b) return
    const ca = a.type === 'device' ? nodeXYToDiscCenter(a.position.x, a.position.y) : { x: a.position.x + (a.width as number || 0) / 2, y: a.position.y + (a.height as number || 0) / 2 }
    const cb = b.type === 'device' ? nodeXYToDiscCenter(b.position.x, b.position.y) : { x: b.position.x + (b.width as number || 0) / 2, y: b.position.y + (b.height as number || 0) / 2 }
    const span = Math.max(Math.hypot(cb.x - ca.x, cb.y - ca.y), 200)
    const rect = wrapperRef.current?.getBoundingClientRect()
    const zoom = Math.min(1.3, Math.max(0.3, ((rect ? Math.min(rect.width, rect.height) : 600) * 0.6) / span))
    setCenter((ca.x + cb.x) / 2, (ca.y + cb.y) / 2, { zoom, duration: 450 })
    setEdges((eds) => eds.map((x) => ({ ...x, selected: x.id === id })))
  }, [setCenter, setEdges])

  const focusProblem = useCallback((p: Problem) => {
    if (p.kind === 'device') { focusNode(p.targetId, false); setLiveFocusId(p.targetId) }
    else focusEdge(p.targetId)
  }, [focusNode, focusEdge])

  const problems = useMemo(() => (liveMode ? computeProblems(detail, nodesLive, liveData) : []), [liveMode, detail, nodesLive, liveData])

  /* ── Selection tracking + keyboard ───────────────────────────── */
  const onSelectionChange = useCallback(({ nodes: sel }: { nodes: Node[] }) => {
    selectedIds.current = new Set(sel.map((n) => n.id))
    setSelCount(sel.length)
    setHasGroupSel(sel.some((n) => !!groupIdOf(n)))
    setSelLocked(sel.length > 0 && sel.every(lockedOf))
    const shapeSel = sel.filter((n) => n.type === 'shape')
    setSelectedShapeId(sel.length === 1 && shapeSel.length === 1 ? shapeSel[0].id : null)
  }, [])

  const wrapperRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = wrapperRef.current
    if (!el) return
    const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') setPanKey(false) }
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t?.tagName === 'INPUT' || t?.tagName === 'TEXTAREA' || t?.tagName === 'SELECT' || t?.isContentEditable) return
      const mod = e.ctrlKey || e.metaKey
      const k = e.key.toLowerCase()
      if (e.code === 'Space') { e.preventDefault(); setPanKey(true) }
      else if (e.key === 'Escape') {
        setMenu(null)
        setLiveFocusId(null)
        setNodes((nds) => nds.some((n) => n.selected) ? nds.map((n) => (n.selected ? { ...n, selected: false } : n)) : nds)
        setEdges((eds) => eds.some((x) => x.selected) ? eds.map((x) => ({ ...x, selected: false })) : eds)
      }
      else if (mod && k === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo() }
      else if (mod && k === 'y') { e.preventDefault(); redo() }
      else if (mod && k === 'd') { e.preventDefault(); if (!liveMode) duplicateSelectedRef.current() }
      else if (mod && k === 'c') { if (!liveMode) { e.preventDefault(); copyRef.current() } }
      else if (mod && k === 'v') { if (!liveMode) { e.preventDefault(); pasteRef.current() } }
      else if (mod && k === 'g') { e.preventDefault(); if (!liveMode) { if (e.shiftKey) ungroupSelectedRef.current(); else groupSelectedRef.current() } }
      else if (mod && k === 'a') { e.preventDefault(); setNodes((nds) => nds.map((n) => ({ ...n, selected: true }))) }
      else if ((e.key === 'Delete' || e.key === 'Backspace') && !liveMode) { e.preventDefault(); deleteSelectedRef.current() }
      else if (!mod && e.key.startsWith('Arrow') && !liveMode) {
        e.preventDefault()
        const step = (e.shiftKey ? 4 : 1) * NUDGE
        nudgeRef.current(e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0, e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0)
      }
      else if (!mod && k === 'v') setTool('select')
      else if (!mod && k === 'c') { if (!liveMode) setTool('connect') }
      else if (!mod && k === 'l') { if (!liveMode) toggleLockRef.current() }
      else if (!mod && (k === 'f' || e.key === '0')) fitView({ padding: 0.2, duration: 300 })
      else if (!mod && (e.key === '+' || e.key === '=')) { const v = getViewport(); setViewport({ ...v, zoom: Math.min(2.5, v.zoom * 1.2) }, { duration: 150 }) }
      else if (!mod && e.key === '-') { const v = getViewport(); setViewport({ ...v, zoom: Math.max(0.15, v.zoom / 1.2) }, { duration: 150 }) }
    }
    el.addEventListener('keydown', onKey)
    el.addEventListener('keyup', onKeyUp)
    return () => { el.removeEventListener('keydown', onKey); el.removeEventListener('keyup', onKeyUp) }
  }, [setNodes, setEdges, liveMode, undo, redo, fitView, getViewport, setViewport])

  /* ── Context menus ───────────────────────────────────────────── */
  const closeMenu = useCallback(() => setMenu(null), [])
  const openDevice = useCallback((deviceId: string) => window.open(`/devices/${deviceId}`, '_blank'), [])

  const onPaneContextMenu = useCallback((e: MouseEvent | React.MouseEvent) => {
    e.preventDefault()
    const cx = (e as MouseEvent).clientX, cy = (e as MouseEvent).clientY
    if (liveMode) {
      setMenu({ x: cx, y: cy, items: [
        { type: 'header', label: 'Live map' },
        { type: 'item', label: 'Fit to screen (F)', icon: <Maximize className="h-4 w-4" />, onClick: () => fitView({ padding: 0.12, duration: 300 }) },
        { type: 'item', label: 'Open active alerts', icon: <ExternalLink className="h-4 w-4" />, onClick: () => navigate('/alerts') },
      ] })
      return
    }
    const sel = nodesRef.current.filter((n) => n.selected).length
    const at = screenToFlowPosition({ x: cx, y: cy })
    const items: MenuItem[] = [{ type: 'header', label: sel > 0 ? `${sel} selected` : 'Add here' }]
    if (sel > 0) {
      items.push({ type: 'item', label: `Delete selected (${sel})`, icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => deleteSelected() })
      items.push({ type: 'item', label: 'Copy (Ctrl+C)', icon: <Copy className="h-4 w-4" />, onClick: () => copySelected() })
      if (sel >= 2) {
        items.push({ type: 'item', label: 'Align left', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => applyAlign('left') })
        items.push({ type: 'item', label: 'Align top', icon: <AlignStartHorizontal className="h-4 w-4" />, onClick: () => applyAlign('top') })
      }
      items.push({ type: 'divider' })
      items.push({ type: 'header', label: 'Add here' })
    }
    for (const p of SHAPE_PRESETS.filter((x) => ['text', 'sticky', 'rectangle', 'zone', 'circle'].includes(x.key))) {
      items.push({ type: 'item', label: p.label, icon: p.icon, onClick: () => addShapeAtFlow(p.spec, at) })
    }
    items.push({ type: 'item', label: 'Network icon…', icon: <LayoutGrid className="h-4 w-4" />, onClick: () => setIconPickAt(at) })
    items.push({ type: 'divider' })
    items.push({ type: 'item', label: 'Paste (Ctrl+V)', icon: <ClipboardPaste className="h-4 w-4" />, disabled: clipboardRef.current.length === 0, onClick: () => paste() })
    items.push({ type: 'item', label: 'Select all (Ctrl+A)', icon: <SquareDashedMousePointer className="h-4 w-4" />, onClick: () => setNodes((nds) => nds.map((n) => ({ ...n, selected: true }))) })
    items.push({ type: 'item', label: 'Fit to screen (F)', icon: <Maximize className="h-4 w-4" />, onClick: () => fitView({ padding: 0.2, duration: 300 }) })
    items.push({ type: 'divider' })
    items.push({ type: 'item', label: 'Auto-align to grid', icon: <Wand2 className="h-4 w-4" />, onClick: autoAlign })
    items.push({ type: 'item', label: 'Tidy layout', icon: <Sparkles className="h-4 w-4" />, onClick: tidyUp })
    setMenu({ x: cx, y: cy, items })
  }, [liveMode, setNodes, autoAlign, applyAlign, tidyUp, fitView, navigate, screenToFlowPosition, addShapeAtFlow, paste, copySelected])

  const onNodeContextMenu = useCallback((e: React.MouseEvent, node: Node) => {
    e.preventDefault()
    const dev = (node.data as any)?.node as ManualMapNode | undefined
    if (liveMode) {
      const items: MenuItem[] = [{ type: 'header', label: dev?.hostname || ((node.data as any)?.shape?.text) || 'Item' }]
      if (dev) {
        items.push({ type: 'item', label: 'Pin details', icon: <LocateFixed className="h-4 w-4" />, onClick: () => setLiveFocusId(node.id) })
        items.push({ type: 'item', label: 'Open device page', icon: <ExternalLink className="h-4 w-4" />, onClick: () => openDevice(dev.device_id) })
        items.push({ type: 'item', label: 'Open interfaces', icon: <Cable className="h-4 w-4" />, onClick: () => window.open(`/devices/${dev.device_id}?tab=interfaces`, '_blank') })
        items.push({ type: 'item', label: 'Open alerts for this device', icon: <ExternalLink className="h-4 w-4" />, onClick: () => window.open(`/alerts?device=${dev.device_id}`, '_blank') })
      }
      items.push({ type: 'item', label: 'Centre on this item', icon: <Maximize className="h-4 w-4" />, onClick: () => focusNode(node.id, false) })
      setMenu({ x: e.clientX, y: e.clientY, items })
      return
    }
    if (!node.selected) setNodes((nds) => nds.map((n) => ({ ...n, selected: n.id === node.id })))
    const selIds = node.selected ? selectedIds.current : new Set([node.id])
    const multi = selIds.size > 1
    const locked = lockedOf(node)
    const items: MenuItem[] = []
    if (multi) {
      const anyGrouped = nodesRef.current.some((n) => n.selected && groupIdOf(n))
      const allLocked = nodesRef.current.filter((n) => n.selected).every(lockedOf)
      items.push({ type: 'header', label: `${selIds.size} selected` })
      items.push({ type: 'item', label: 'Copy (Ctrl+C)', icon: <Copy className="h-4 w-4" />, onClick: () => copySelected() })
      items.push({ type: 'item', label: 'Duplicate selection (Ctrl+D)', icon: <CopyPlus className="h-4 w-4" />, onClick: () => { void duplicateSelectedRef.current() } })
      items.push({ type: 'item', label: 'Group (Ctrl+G)', icon: <Group className="h-4 w-4" />, onClick: () => groupSelected() })
      if (anyGrouped) items.push({ type: 'item', label: 'Ungroup (Ctrl+Shift+G)', icon: <Ungroup className="h-4 w-4" />, onClick: () => ungroupSelected() })
      items.push({ type: 'item', label: allLocked ? 'Unlock (L)' : 'Lock (L)', icon: allLocked ? <LockOpen className="h-4 w-4" /> : <Lock className="h-4 w-4" />, onClick: () => toggleLockSelected() })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Increase tile size', icon: <Expand className="h-4 w-4" />, onClick: () => resizeSelected(1.15) })
      items.push({ type: 'item', label: 'Decrease tile size', icon: <Shrink className="h-4 w-4" />, onClick: () => resizeSelected(1 / 1.15) })
      items.push({ type: 'item', label: 'Larger icon', icon: <ZoomIn className="h-4 w-4" />, onClick: () => resizeIconFill(1.08) })
      items.push({ type: 'item', label: 'Smaller icon', icon: <ZoomOut className="h-4 w-4" />, onClick: () => resizeIconFill(1 / 1.08) })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Align left', icon: <AlignStartVertical className="h-4 w-4" />, onClick: () => applyAlign('left') })
      items.push({ type: 'item', label: 'Align right', icon: <AlignEndVertical className="h-4 w-4" />, onClick: () => applyAlign('right') })
      items.push({ type: 'item', label: 'Align top', icon: <AlignStartHorizontal className="h-4 w-4" />, onClick: () => applyAlign('top') })
      items.push({ type: 'item', label: 'Align bottom', icon: <AlignEndHorizontal className="h-4 w-4" />, onClick: () => applyAlign('bottom') })
      items.push({ type: 'item', label: 'Center horizontally', icon: <AlignHorizontalJustifyCenter className="h-4 w-4" />, onClick: () => applyAlign('center-h') })
      items.push({ type: 'item', label: 'Center vertically', icon: <AlignVerticalJustifyCenter className="h-4 w-4" />, onClick: () => applyAlign('center-v') })
      items.push({ type: 'item', label: 'Distribute horizontally', icon: <AlignHorizontalDistributeCenter className="h-4 w-4" />, onClick: () => applyAlign('distribute-h') })
      items.push({ type: 'item', label: 'Distribute vertically', icon: <AlignVerticalDistributeCenter className="h-4 w-4" />, onClick: () => applyAlign('distribute-v') })
      items.push({ type: 'divider' })
      if (dev) items.push({ type: 'item', label: 'Match sizes to this device', icon: <Sparkles className="h-4 w-4" />, onClick: () => matchSizes(dev) })
      items.push({ type: 'item', label: 'Reset label positions', onClick: () => resetLabels() })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Delete selected', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => deleteSelected() })
    } else if (node.type === 'shape') {
      const sh = (node.data as any).shape as MapShape
      items.push({ type: 'header', label: sh.text ? sh.text.slice(0, 24) : 'Annotation' })
      items.push({ type: 'item', label: 'Copy (Ctrl+C)', icon: <Copy className="h-4 w-4" />, onClick: () => copySelected() })
      items.push({ type: 'item', label: 'Duplicate (Ctrl+D)', icon: <CopyPlus className="h-4 w-4" />, onClick: () => duplicateShape(node.id) })
      items.push({ type: 'item', label: locked ? 'Unlock (L)' : 'Lock (L)', icon: locked ? <LockOpen className="h-4 w-4" /> : <Lock className="h-4 w-4" />, onClick: () => setLockedFor([node.id], !locked) })
      items.push({ type: 'item', label: 'Increase size', icon: <Expand className="h-4 w-4" />, onClick: () => resizeSelected(1.15) })
      items.push({ type: 'item', label: 'Decrease size', icon: <Shrink className="h-4 w-4" />, onClick: () => resizeSelected(1 / 1.15) })
      if (groupIdOf(node)) items.push({ type: 'item', label: 'Ungroup', icon: <Ungroup className="h-4 w-4" />, onClick: () => ungroupSelected() })
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
      items.push({ type: 'item', label: 'Edit device & profile…', icon: <Pencil className="h-4 w-4" />, onClick: () => setEditNode(dev!) })
      items.push({ type: 'item', label: 'Connect from here (C)', icon: <Cable className="h-4 w-4" />, onClick: () => setTool('connect') })
      items.push({ type: 'item', label: 'Copy (Ctrl+C)', icon: <Copy className="h-4 w-4" />, onClick: () => copySelected() })
      items.push({ type: 'item', label: 'Duplicate (Ctrl+D)', icon: <CopyPlus className="h-4 w-4" />, onClick: () => { void duplicateDevice(node.id).then((id) => { if (id) toast.success('Device duplicated') }) } })
      items.push({ type: 'item', label: locked ? 'Unlock (L)' : 'Lock (L)', icon: locked ? <LockOpen className="h-4 w-4" /> : <Lock className="h-4 w-4" />, onClick: () => setLockedFor([node.id], !locked) })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Increase tile size', icon: <Expand className="h-4 w-4" />, onClick: () => resizeSelected(1.15) })
      items.push({ type: 'item', label: 'Decrease tile size', icon: <Shrink className="h-4 w-4" />, onClick: () => resizeSelected(1 / 1.15) })
      items.push({ type: 'item', label: 'Larger icon', icon: <ZoomIn className="h-4 w-4" />, onClick: () => resizeIconFill(1.08) })
      items.push({ type: 'item', label: 'Smaller icon', icon: <ZoomOut className="h-4 w-4" />, onClick: () => resizeIconFill(1 / 1.08) })
      items.push({ type: 'item', label: 'Reset label position', onClick: () => resetLabels() })
      if (groupIdOf(node)) items.push({ type: 'item', label: 'Ungroup', icon: <Ungroup className="h-4 w-4" />, onClick: () => ungroupSelected() })
      if (dev?.device_id) items.push({ type: 'item', label: 'Open device page', icon: <ExternalLink className="h-4 w-4" />, onClick: () => openDevice(dev.device_id) })
      items.push({ type: 'divider' })
      items.push({ type: 'item', label: 'Remove from map', icon: <Trash2 className="h-4 w-4" />, danger: true, onClick: () => setPendingDelete({
        title: 'Remove node?',
        description: `Remove "${dev?.hostname || 'this node'}" from this map? The device itself is not affected.`,
        run: () => { deleteNode.mutate(node.id); setNodes((nds) => nds.filter((n) => n.id !== node.id)) },
      }) })
    }
    setMenu({ x: e.clientX, y: e.clientY, items })
  }, [liveMode, setNodes, applyAlign, deleteNode, deleteShape, bumpShapeZ, matchSizes, resetLabels, duplicateShape, duplicateDevice, groupSelected, ungroupSelected, resizeSelected, resizeIconFill, copySelected, setLockedFor, toggleLockSelected, focusNode, openDevice])

  const onEdgeContextMenu = useCallback((e: React.MouseEvent, edge: Edge) => {
    e.preventDefault()
    const dd = edge.data as any
    const link = dd.link as ManualMapLink
    const isAnnotation = !!dd.annotation
    if (liveMode) {
      const s = nodeById.get(link.source_node_id), t = nodeById.get(link.target_node_id)
      const items: MenuItem[] = [{ type: 'header', label: link?.label || 'Link' }]
      if (s) items.push({ type: 'item', label: `Open ${s.label || s.hostname}`, icon: <ExternalLink className="h-4 w-4" />, onClick: () => openDevice(s.device_id) })
      if (t) items.push({ type: 'item', label: `Open ${t.label || t.hostname}`, icon: <ExternalLink className="h-4 w-4" />, onClick: () => openDevice(t.device_id) })
      items.push({ type: 'item', label: 'Centre on this link', icon: <Maximize className="h-4 w-4" />, onClick: () => focusEdge(edge.id) })
      setMenu({ x: e.clientX, y: e.clientY, items })
      return
    }
    const shape = link?.metadata?.shape || 'curve'
    const shapeItem = (label: string, val: 'curve' | 'straight' | 'orthogonal'): MenuItem =>
      ({ type: 'item', label: `${shape === val ? '✓ ' : '   '}${label}`, onClick: () => setEdgeShape(edge.id, val) })
    const items: MenuItem[] = [
      { type: 'header', label: link?.label || (isAnnotation ? 'Connection' : 'Link') },
      { type: 'item', label: 'Edit link… (ports, colour, arrows)', icon: <Pencil className="h-4 w-4" />, onClick: () => openEditLink(edge) },
      { type: 'item', label: 'Duplicate', icon: <CopyPlus className="h-4 w-4" />, onClick: () => duplicateEdge(edge) },
      { type: 'divider' },
      { type: 'header', label: 'Shape' },
      shapeItem('Curved', 'curve'),
      shapeItem('Straight', 'straight'),
      shapeItem('Orthogonal', 'orthogonal'),
      { type: 'divider' },
      { type: 'item', label: 'Thicker line', icon: <Expand className="h-4 w-4" />, onClick: () => resizeLink(edge.id, 1.25) },
      { type: 'item', label: 'Thinner line', icon: <Shrink className="h-4 w-4" />, onClick: () => resizeLink(edge.id, 1 / 1.25) },
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
    setMenu({ x: e.clientX, y: e.clientY, items })
  }, [liveMode, setEdgeShape, setEdgeWaypoints, resetIfaceLabels, deleteLink, deleteAnnotationLink, setEdges, duplicateEdge, nodeById, resizeLink, openEditLink, openDevice, focusEdge])

  const onNodeDoubleClick = useCallback((_e: React.MouseEvent, node: Node) => {
    if (node.type !== 'device') return
    const dev = (node.data as any).node as ManualMapNode
    if (liveMode) openDevice(dev.device_id)
    else setEditNode(dev)
  }, [liveMode, openDevice])

  const onNodeClick = useCallback((_e: React.MouseEvent, node: Node) => {
    if (liveMode && node.type === 'device') setLiveFocusId(node.id)
  }, [liveMode])

  const onEdgeDoubleClick = useCallback((_e: React.MouseEvent, edge: Edge) => {
    if (!liveMode && !edge.selected) openEditLink(edge)
  }, [liveMode, openEditLink])

  const onEdgeClick = useCallback((_e: React.MouseEvent, edge: Edge) => {
    setEdges((eds) => eds.map((x) => (x.selected === (x.id === edge.id) ? x : { ...x, selected: x.id === edge.id })))
    setNodes((nds) => nds.some((n) => n.selected) ? nds.map((n) => (n.selected ? { ...n, selected: false } : n)) : nds)
  }, [setEdges, setNodes])

  /* ── Group resize ───────────────────────────────────────────────────────── */
  const groupItems = useMemo(
    () => nodes.filter((n) => n.selected && !lockedOf(n)).map((n) => ({
      id: n.id, x: n.position.x, y: n.position.y,
      w: typeof n.width === 'number' ? n.width : 128,
      h: typeof n.height === 'number' ? n.height : 64,
    })),
    [nodes],
  )
  const groupPrevRef = useRef<{ id: string; x: number; y: number }[] | null>(null)
  const applyGroup = useCallback((moves: { id: string; x: number; y: number }[], commit: boolean) => {
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
    if (!sel.length) {
      // Nothing selected on the canvas — maybe a cable is.
      const edge = edgesRef.current.find((e) => e.selected)
      if (!edge) return
      const isAnnotation = !!(edge.data as any)?.annotation
      setPendingDelete({
        title: isAnnotation ? 'Delete connection?' : 'Delete link?',
        description: 'This removes the connection between the two items.',
        run: () => { if (isAnnotation) deleteAnnotationLink(edge.id); else { deleteLink.mutate(edge.id); setEdges((eds) => eds.filter((x) => x.id !== edge.id)) } },
      })
      return
    }
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
  }, [deleteNode, deleteShape, deleteLink, deleteAnnotationLink, setNodes, setEdges])
  deleteSelectedRef.current = deleteSelected

  // Toolbar dropdowns reuse the context-menu renderer anchored under a button.
  const openMenuUnder = useCallback((el: HTMLElement, items: MenuItem[]) => {
    const r = el.getBoundingClientRect()
    setMenu({ x: r.left, y: r.bottom + 4, items })
  }, [])

  const arrangeItems = useCallback((): MenuItem[] => {
    const n = selCount
    return [
      { type: 'header', label: n ? `${n} selected` : 'Arrange' },
      { type: 'item', label: 'Align left', icon: <AlignStartVertical className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('left') },
      { type: 'item', label: 'Align right', icon: <AlignEndVertical className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('right') },
      { type: 'item', label: 'Align top', icon: <AlignStartHorizontal className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('top') },
      { type: 'item', label: 'Align bottom', icon: <AlignEndHorizontal className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('bottom') },
      { type: 'item', label: 'Center horizontally', icon: <AlignHorizontalJustifyCenter className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('center-h') },
      { type: 'item', label: 'Center vertically', icon: <AlignVerticalJustifyCenter className="h-4 w-4" />, disabled: n < 2, onClick: () => applyAlign('center-v') },
      { type: 'item', label: 'Distribute horizontally', icon: <AlignHorizontalDistributeCenter className="h-4 w-4" />, disabled: n < 3, onClick: () => applyAlign('distribute-h') },
      { type: 'item', label: 'Distribute vertically', icon: <AlignVerticalDistributeCenter className="h-4 w-4" />, disabled: n < 3, onClick: () => applyAlign('distribute-v') },
      { type: 'divider' },
      { type: 'item', label: 'Group (Ctrl+G)', icon: <Group className="h-4 w-4" />, disabled: n < 2, onClick: groupSelected },
      { type: 'item', label: 'Ungroup (Ctrl+Shift+G)', icon: <Ungroup className="h-4 w-4" />, disabled: !hasGroupSel, onClick: ungroupSelected },
      { type: 'item', label: selLocked ? 'Unlock (L)' : 'Lock (L)', icon: selLocked ? <LockOpen className="h-4 w-4" /> : <Lock className="h-4 w-4" />, disabled: n < 1, onClick: toggleLockSelected },
      { type: 'divider' },
      { type: 'item', label: 'Increase tile size', icon: <Expand className="h-4 w-4" />, disabled: n < 1, onClick: () => resizeSelected(1.15) },
      { type: 'item', label: 'Decrease tile size', icon: <Shrink className="h-4 w-4" />, disabled: n < 1, onClick: () => resizeSelected(1 / 1.15) },
      { type: 'item', label: 'Larger icon', icon: <ZoomIn className="h-4 w-4" />, disabled: n < 1, onClick: () => resizeIconFill(1.08) },
      { type: 'item', label: 'Smaller icon', icon: <ZoomOut className="h-4 w-4" />, disabled: n < 1, onClick: () => resizeIconFill(1 / 1.08) },
      { type: 'divider' },
      { type: 'item', label: 'Auto-align all to grid', icon: <Wand2 className="h-4 w-4" />, onClick: autoAlign },
      { type: 'item', label: 'Tidy layout', icon: <Sparkles className="h-4 w-4" />, onClick: tidyUp },
    ]
  }, [selCount, hasGroupSel, selLocked, applyAlign, groupSelected, ungroupSelected, toggleLockSelected, resizeSelected, resizeIconFill, autoAlign, tidyUp])

  const viewItems = useCallback((): MenuItem[] => [
    { type: 'header', label: 'View' },
    { type: 'item', label: `${smartOn ? '✓ ' : '   '}Smart guides`, icon: <Ruler className="h-4 w-4" />, onClick: () => setSmartOn((v) => !v) },
    { type: 'item', label: `${snapOn ? '✓ ' : '   '}Snap to grid`, icon: <Magnet className="h-4 w-4" />, onClick: () => setSnapOn((v) => !v) },
    { type: 'item', label: `${gridOn ? '✓ ' : '   '}Show grid`, icon: <Grid3x3 className="h-4 w-4" />, onClick: () => setGridOn((v) => !v) },
    { type: 'divider' },
    { type: 'item', label: 'Fit to screen (F)', icon: <Maximize className="h-4 w-4" />, onClick: () => fitView({ padding: 0.2, duration: 300 }) },
    { type: 'item', label: 'Zoom 100%', icon: <ZoomIn className="h-4 w-4" />, onClick: () => { const v = getViewport(); setViewport({ ...v, zoom: 1 }, { duration: 200 }) } },
  ], [smartOn, snapOn, gridOn, fitView, getViewport, setViewport])

  const liveFocusNode = liveFocusId ? nodeById.get(liveFocusId) : undefined

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
        onReconnect={onReconnect}
        edgesReconnectable={!liveMode && tool === 'select'}
        reconnectRadius={14}
        connectionRadius={44}
        connectionMode={ConnectionMode.Loose}
        onSelectionChange={onSelectionChange}
        onPaneContextMenu={onPaneContextMenu}
        onNodeContextMenu={onNodeContextMenu}
        onNodeDoubleClick={onNodeDoubleClick}
        onNodeClick={onNodeClick}
        onEdgeContextMenu={onEdgeContextMenu}
        onEdgeClick={onEdgeClick}
        onEdgeDoubleClick={onEdgeDoubleClick}
        onPaneClick={() => { closeMenu(); setLiveFocusId(null); setEdges((eds) => eds.some((e) => e.selected) ? eds.map((e) => ({ ...e, selected: false })) : eds) }}
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
        selectionOnDrag={!liveMode && tool === 'select' && !panKey}
        panOnDrag={(!liveMode && tool === 'select' && !panKey) ? [1] : true}
        nodesDraggable={!liveMode && tool === 'select'}
        nodesConnectable={!liveMode}
        elementsSelectable={!liveMode && tool === 'select'}
        elevateEdgesOnSelect
        proOptions={{ hideAttribution: true }}
      >
        {gridOn && <Background variant={BackgroundVariant.Dots} gap={26} size={1} color={theme === 'dark' ? 'rgba(148,163,184,0.18)' : 'rgba(71,85,105,0.22)'} />}

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
          nodeColor={(n) => (n.type === 'shape' ? (theme === 'dark' ? 'rgba(148,163,184,0.18)' : 'rgba(71,85,105,0.18)') : (STATUS_HEX[statusKey((n.data as any)?.node?.status)] || STATUS_HEX.unknown))}
          nodeStrokeWidth={2}
          maskColor={theme === 'dark' ? 'rgba(2,6,23,0.6)' : 'rgba(226,232,240,0.6)'}
          className="!bg-surface"
        />

        {/* NOC overlays (live mode) */}
        {liveMode && (
          <Panel position="top-center">
            <NocStatusBar detail={detail} nodesLive={nodesLive} liveData={liveData} updatedAt={liveUpdatedAt || Date.now()} onOpenAlerts={() => navigate('/alerts')} />
          </Panel>
        )}
        {liveMode && (
          <Panel position="bottom-center">
            <MapLegend />
          </Panel>
        )}
        {liveMode && (
          <Panel position="top-right" className="flex flex-col items-end gap-2">
            <FindBox nodes={detail.nodes} onPick={(id) => focusNode(id, false)} />
            {showProblems && <ProblemsPanel problems={problems} onFocus={focusProblem} />}
            {liveFocusNode && (
              <div className="relative">
                <button type="button" onClick={() => setLiveFocusId(null)} className="absolute -right-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface text-muted shadow hover:text-text" title="Close">
                  <X className="h-3 w-3" />
                </button>
                <DeviceHoverCard node={liveFocusNode} nodeLive={nodesLive[liveFocusNode.id]} sk={statusKey(nodesLive[liveFocusNode.id]?.status ?? liveFocusNode.status)} />
                <div className="mt-1 flex gap-1">
                  <button type="button" onClick={() => openDevice(liveFocusNode.device_id)} className="flex-1 rounded-md border border-border bg-surface/95 px-2 py-1 text-[10px] font-semibold text-text2 shadow hover:border-primary/50 hover:text-text">Device page</button>
                  <button type="button" onClick={() => window.open(`/alerts?device=${liveFocusNode.device_id}`, '_blank')} className="flex-1 rounded-md border border-border bg-surface/95 px-2 py-1 text-[10px] font-semibold text-text2 shadow hover:border-primary/50 hover:text-text">Alerts</button>
                </div>
              </div>
            )}
          </Panel>
        )}

        {/* Floating editor toolbar (design) */}
        {!liveMode && (
          <Panel position="top-left" className="max-w-[calc(100%-1rem)]">
            <div className="flex flex-wrap items-center gap-1 rounded-lg border border-border bg-surface/90 p-1 shadow-lg backdrop-blur">
              <ToolBtn active={tool === 'select'} onClick={() => setTool('select')} title="Select / box-select (V) · hold Space to pan"><MousePointer2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn active={tool === 'connect'} onClick={() => setTool('connect')} title="Connect / draw cable (C) — or drag the blue plug that appears when hovering a device"><Cable className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={quickConnect} disabled={autoConnect.isPending} title="Quick connect — auto-draw CDP/LLDP links between placed devices"><Zap className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn onClick={undo} title="Undo (Ctrl+Z)"><Undo2 className="h-4 w-4" /></ToolBtn>
              <ToolBtn onClick={redo} title="Redo (Ctrl+Shift+Z)"><Redo2 className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <InsertMenu onAdd={addShapeAt} />
              <MenuBtn label="Arrange" onOpen={(el) => openMenuUnder(el, arrangeItems())} />
              <MenuBtn label="View" onOpen={(el) => openMenuUnder(el, viewItems())} />
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('left')} title="Align left"><AlignStartVertical className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('top')} title="Align top"><AlignStartHorizontal className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 2} onClick={() => applyAlign('center-h')} title="Center horizontally"><AlignHorizontalJustifyCenter className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 3} onClick={() => applyAlign('distribute-h')} title="Distribute horizontally"><AlignHorizontalDistributeCenter className="h-4 w-4" /></ToolBtn>
              <div className="mx-0.5 h-5 w-px bg-border" />
              <ToolBtn disabled={selCount < 1} active={selLocked} onClick={toggleLockSelected} title={selLocked ? 'Unlock selection (L)' : 'Lock selection (L)'}>{selLocked ? <Lock className="h-4 w-4" /> : <LockOpen className="h-4 w-4" />}</ToolBtn>
              <ToolBtn disabled={selCount < 1} onClick={copySelected} title="Copy (Ctrl+C)"><Copy className="h-4 w-4" /></ToolBtn>
              <ToolBtn disabled={selCount < 1} danger onClick={deleteSelected} title="Delete selected (Del)"><Trash2 className="h-4 w-4" /></ToolBtn>
              {selCount > 0 && <span className="px-1.5 text-[11px] font-semibold text-muted">{selCount} sel</span>}
              <div className="mx-0.5 h-5 w-px bg-border" />
              <FindBox nodes={detail.nodes} onPick={(id) => focusNode(id, true)} compact />
              <div className="mx-0.5 h-5 w-px bg-border" />
              <span
                className={cn('flex items-center gap-1 px-1.5 text-[10px] font-semibold', pendingMutations > 0 ? 'text-warning' : 'text-success/80')}
                title={pendingMutations > 0 ? 'Saving changes…' : 'All changes saved automatically'}
              >
                {pendingMutations > 0 ? <CloudUpload className="h-3.5 w-3.5 animate-pulse" /> : <Check className="h-3.5 w-3.5" />}
                {pendingMutations > 0 ? 'Saving…' : 'Saved'}
              </span>
            </div>
          </Panel>
        )}

        {/* Annotation style inspector (single shape selected) */}
        {!liveMode && selectedShape && (
          <Panel position="top-right">
            <ShapeInspector
              shape={selectedShape}
              devices={detail.nodes.map((n) => ({ hostname: n.label || n.hostname, ip: n.ip_address }))}
              onChange={(patch, commit) => setShape(selectedShape.id, patch, commit)}
              onZ={(dir) => bumpShapeZ(selectedShape.id, dir)}
              onLock={(locked) => setLockedFor([selectedShape.id], locked)}
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

      {iconPickAt && (
        <IconPickerDialog onCancel={() => setIconPickAt(null)} onPick={(icon) => { const at = iconPickAt; setIconPickAt(null); addShapeAtFlow(iconSpec(icon), at) }} />
      )}

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
          key={`${editLink.link.id}-${editLink.link.source_node_id}-${editLink.link.target_node_id}`}
          link={editLink.link}
          source={editLink.source ?? nodeById.get(editLink.link.source_node_id)}
          target={editLink.target ?? nodeById.get(editLink.link.target_node_id)}
          sourceLabel={editLink.sourceLabel}
          targetLabel={editLink.targetLabel}
          saving={updateLink.isPending}
          onCancel={() => setEditLink(null)}
          onSave={(patch) => saveLink(editLink.link.id, patch, editLink.annotation)}
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

function MenuBtn({ label, onOpen }: { label: string; onOpen: (el: HTMLElement) => void }) {
  return (
    <button
      type="button"
      onClick={(e) => onOpen(e.currentTarget)}
      className="flex h-7 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted transition hover:bg-primary/10 hover:text-text"
    >
      {label} <ChevronDown className="h-3 w-3" />
    </button>
  )
}

/** Find a device on the map by name/IP and jump to it. */
function FindBox({ nodes, onPick, compact }: { nodes: ManualMapNode[]; onPick: (id: string) => void; compact?: boolean }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const matches = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return []
    return nodes.filter((n) => (n.label || '').toLowerCase().includes(s) || n.hostname.toLowerCase().includes(s) || (n.ip_address || '').includes(s)).slice(0, 8)
  }, [q, nodes])
  const pick = (id: string) => { onPick(id); setOpen(false); setQ('') }
  return (
    <div className={cn('relative', compact ? 'w-40' : 'w-72 max-w-[80vw]')}>
      <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
      <input
        value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => { if (e.key === 'Enter' && matches[0]) pick(matches[0].id); if (e.key === 'Escape') { setQ(''); setOpen(false) } }}
        placeholder="Find on map…"
        className={cn('w-full rounded-md border border-border bg-surface/95 pl-7 pr-2 text-xs text-text shadow-sm outline-none backdrop-blur focus:border-primary/60', compact ? 'h-7' : 'h-8')}
      />
      {open && matches.length > 0 && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-md border border-border bg-surface shadow-xl">
          {matches.map((n) => (
            <button key={n.id} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(n.id)} className="flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left text-xs hover:bg-primary/10">
              <span className="truncate font-medium text-text">{n.label || n.hostname}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted">{n.ip_address}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
