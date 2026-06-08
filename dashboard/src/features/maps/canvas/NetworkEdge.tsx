import { memo } from 'react'
import { EdgeLabelRenderer, type EdgeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import {
  LINK_KIND_STYLE,
  STATUS_COLOR,
  edgePath,
  formatBps,
  linkHealth,
  linkKindOf,
  linkShapeOf,
  linkWaypoints,
  pctToPx,
  utilizationColor,
  type LiveLinkData,
  type ManualMapLink,
  type NodeStatus,
} from '../core'

export type NetworkEdgeData = {
  link: ManualMapLink
  sourceStatus: NodeStatus
  targetStatus: NodeStatus
  live?: LiveLinkData
  liveMode: boolean
  showThroughput: boolean
}

/* React Flow custom edge — faithful port of v1's link rendering: status- or
 * utilization-coloured stroke, kind-specific dash/accent, animated flow in
 * live mode, and interface/throughput chips. Geometry uses the shared
 * edgePath() in logical-pixel space (sourceX/Y, targetX/Y come from the disc-
 * centre handles). */
function NetworkEdgeImpl({ sourceX, sourceY, targetX, targetY, data, selected }: EdgeProps) {
  const d = data as NetworkEdgeData
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput } = d

  const health = linkHealth(sourceStatus, targetStatus)
  const color = STATUS_COLOR[health].line
  const kind = linkKindOf(link)
  const shape = linkShapeOf(link)
  const kindStyle = LINK_KIND_STYLE[kind] || {}
  const wps = linkWaypoints(link).map((w) => pctToPx(w))
  const path = edgePath(shape, sourceX, sourceY, targetX, targetY, wps)

  const animate = liveMode && (health === 'up' || health === 'degraded')
  const baseWidth = (kindStyle.widthMul || 1) * 3
  const flowWidth = (kindStyle.widthMul || 1) * 1.5

  const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
  const utilStroke = live && utilPct != null ? utilizationColor(utilPct) : null

  const srcIf = link.metadata?.src_interface || live?.source.if_name
  const dstIf = link.metadata?.dst_interface || live?.target.if_name
  const bps = live ? Math.max(live.source.in_bps || 0, live.source.out_bps || 0, live.target.in_bps || 0, live.target.out_bps || 0) : 0

  return (
    <>
      {/* Wide invisible hit area */}
      <path d={path.d} fill="none" stroke="transparent" strokeWidth={14} vectorEffect="non-scaling-stroke" className="react-flow__edge-interaction" />
      {/* Accent halo (fiber/vpn glow + selection highlight) */}
      {(kindStyle.accent || selected) && (
        <path
          d={path.d}
          fill="none"
          stroke={selected ? 'rgb(var(--primary))' : (kindStyle.accent as string)}
          strokeOpacity={selected ? 0.6 : 0.45}
          strokeWidth={selected ? baseWidth + 3 : baseWidth + 1.5}
          vectorEffect="non-scaling-stroke"
        />
      )}
      {/* Base stroke */}
      <path
        d={path.d}
        fill="none"
        vectorEffect="non-scaling-stroke"
        strokeWidth={baseWidth}
        strokeDasharray={kindStyle.dash}
        className={cn(utilStroke || color, 'opacity-70')}
      />
      {/* Animated flow inner stroke */}
      {animate && (
        <path
          d={path.d}
          fill="none"
          vectorEffect="non-scaling-stroke"
          strokeWidth={flowWidth}
          className={cn(utilStroke || color, utilPct != null && utilPct >= 60 ? 'nm-flow' : 'nm-flow-slow')}
        />
      )}

      <EdgeLabelRenderer>
        {srcIf && (
          <EdgeChip x={path.near.x} y={path.near.y} variant="iface">{srcIf}</EdgeChip>
        )}
        {dstIf && (
          <EdgeChip x={path.far.x} y={path.far.y} variant="iface">{dstIf}</EdgeChip>
        )}
        {showThroughput && live && bps > 0 && (
          <EdgeChip
            x={path.mid.x}
            y={path.mid.y}
            variant="live"
            tone={utilPct != null && utilPct >= 85 ? 'danger' : utilPct != null && utilPct >= 60 ? 'warning' : 'success'}
          >
            {formatBps(bps)}{utilPct != null && utilPct > 0 ? ` · ${utilPct.toFixed(0)}%` : ''}
          </EdgeChip>
        )}
      </EdgeLabelRenderer>
    </>
  )
}

function EdgeChip({
  x, y, variant, tone, children,
}: {
  x: number
  y: number
  variant: 'iface' | 'live'
  tone?: 'success' | 'warning' | 'danger'
  children: React.ReactNode
}) {
  const cls =
    variant === 'iface'
      ? 'bg-surface/95 text-text2 border-border'
      : tone === 'danger'
        ? 'bg-danger/15 text-danger border-danger/40'
        : tone === 'warning'
          ? 'bg-warning/15 text-warning border-warning/40'
          : 'bg-success/15 text-success border-success/40'
  return (
    <div
      className="nodrag nopan pointer-events-none absolute"
      style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}
    >
      <div className={cn('rounded border px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight shadow-sm backdrop-blur', cls)}>
        {children}
      </div>
    </div>
  )
}

export const NetworkEdge = memo(NetworkEdgeImpl)
