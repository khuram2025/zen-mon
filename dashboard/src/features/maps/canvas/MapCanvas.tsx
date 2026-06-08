import { useEffect, useMemo } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  discCenterToNodeXY,
  nodeXYToDiscCenter,
  pctToPx,
  pxToPct,
  statusKey,
  type LiveLinkData,
  type ManualMapDetail,
} from '../core'
import { DeviceNode } from './DeviceNode'
import { NetworkEdge } from './NetworkEdge'

// Stable type maps (must not be re-created each render).
const nodeTypes = { device: DeviceNode }
const edgeTypes = { network: NetworkEdge }

const STATUS_HEX: Record<string, string> = {
  up: '#22c55e',
  down: '#ef4444',
  degraded: '#f59e0b',
  maintenance: '#3b82f6',
  unknown: '#6b7280',
}

export type MapCanvasProps = {
  detail: ManualMapDetail
  liveData: Record<string, LiveLinkData>
  liveMode: boolean
  showThroughput: boolean
  onPersistPosition: (id: string, x_pct: number, y_pct: number) => void
}

export function MapCanvas({ detail, liveData, liveMode, showThroughput, onPersistPosition }: MapCanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const nodeById = useMemo(() => new Map(detail.nodes.map((n) => [n.id, n])), [detail.nodes])

  // Build/refresh nodes when the server node set or the live flag changes.
  useEffect(() => {
    setNodes(
      detail.nodes.map((n) => {
        const c = pctToPx(n)
        return {
          id: n.id,
          type: 'device',
          position: discCenterToNodeXY(c.x, c.y),
          data: { node: n, live: liveMode },
          draggable: !liveMode,
        } satisfies Node
      }),
    )
  }, [detail.nodes, liveMode, setNodes])

  // Build/refresh edges when links, live data, or display flags change.
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
            },
          } satisfies Edge
        }),
    )
  }, [detail.links, liveData, liveMode, showThroughput, nodeById, setEdges])

  const handleDragStop: NodeMouseHandler = (_evt, node) => {
    const c = nodeXYToDiscCenter(node.position.x, node.position.y)
    const pct = pxToPct(c.x, c.y)
    onPersistPosition(node.id, pct.x_pct, pct.y_pct)
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeDragStop={handleDragStop}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      colorMode="dark"
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.15}
      maxZoom={2.5}
      nodesDraggable={!liveMode}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={26} size={1} className="!bg-bg" color="rgba(148,163,184,0.18)" />
      <Controls showInteractive={false} />
      <MiniMap
        pannable
        zoomable
        nodeColor={(n) => STATUS_HEX[statusKey((n.data as any)?.node?.status)] || STATUS_HEX.unknown}
        nodeStrokeWidth={2}
        maskColor="rgba(2,6,23,0.6)"
        className="!bg-surface"
      />
    </ReactFlow>
  )
}
