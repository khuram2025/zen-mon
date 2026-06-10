/* Smart alignment guides ("magnetic" snaplines) + layout tidy heuristics.
 *
 * Pure geometry — no React. While a node is dragged the canvas calls
 * computeSmartSnap() each frame with the dragged box and every other node's
 * box; it returns a position correction (snap) plus the guide lines and
 * equal-spacing hints to render. Devices snap by their disc CENTRE (the
 * visual anchor); shapes also snap by their edges. */

import type { Node } from '@xyflow/react'
import { DISC_CX, DISC_CY } from '../core'
import { snap } from './align'

export type GuideBox = {
  id: string
  x: number
  y: number
  w: number
  h: number
  /** Devices snap centre-to-centre; shapes also expose their edges. */
  kind: 'device' | 'shape'
}

export type GuideLine = { coord: number; from: number; to: number }
export type GapHint = { x1: number; y1: number; x2: number; y2: number }

export type SmartSnapResult = {
  dx: number
  dy: number
  v: GuideLine[] // vertical lines (x = coord, spanning y from..to)
  h: GuideLine[] // horizontal lines (y = coord, spanning x from..to)
  gaps: GapHint[] // equal-spacing indicator segments
}

export const NO_SNAP: SmartSnapResult = { dx: 0, dy: 0, v: [], h: [], gaps: [] }

export function nodeToGuideBox(n: Node): GuideBox {
  return {
    id: n.id,
    x: n.position.x,
    y: n.position.y,
    w: n.measured?.width ?? (typeof n.width === 'number' ? n.width : 128),
    h: n.measured?.height ?? (typeof n.height === 'number' ? n.height : 64),
    kind: n.type === 'shape' ? 'shape' : 'device',
  }
}

const cx = (b: GuideBox) => (b.kind === 'device' ? b.x + DISC_CX : b.x + b.w / 2)
const cy = (b: GuideBox) => (b.kind === 'device' ? b.y + DISC_CY : b.y + b.h / 2)

/** Snap-relevant x coordinates of a box (value + which part it is). */
function xTargets(b: GuideBox): number[] {
  return b.kind === 'device' ? [cx(b)] : [b.x, cx(b), b.x + b.w]
}
function yTargets(b: GuideBox): number[] {
  return b.kind === 'device' ? [cy(b)] : [b.y, cy(b), b.y + b.h]
}

/** Closest alignment between any of the dragged box's anchors and any other
 *  box's anchors on one axis. Returns the correction and the guide coord. */
function bestAxisSnap(
  dragAnchors: number[],
  others: { box: GuideBox; targets: number[] }[],
  tol: number,
): { delta: number; coord: number; matched: GuideBox[] } | null {
  let best: { delta: number; coord: number; matched: GuideBox[] } | null = null
  for (const da of dragAnchors) {
    for (const o of others) {
      for (const t of o.targets) {
        const delta = t - da
        if (Math.abs(delta) > tol) continue
        if (!best || Math.abs(delta) < Math.abs(best.delta)) {
          best = { delta, coord: t, matched: [o.box] }
        } else if (best && Math.abs(delta - best.delta) < 0.01 && t === best.coord) {
          best.matched.push(o.box)
        }
      }
    }
  }
  return best
}

/** Equal-spacing snap along one axis: if the dragged centre is close to the
 *  position where its gap to the nearest neighbour equals that neighbour's
 *  gap to ITS neighbour, snap to make the three perfectly even. */
function equalGapSnap(
  dragC: number,
  rowMates: GuideBox[],
  axis: 'x' | 'y',
  tol: number,
): { delta: number; centers: number[] } | null {
  const centers = rowMates.map((b) => (axis === 'x' ? cx(b) : cy(b))).sort((a, b) => a - b)
  if (centers.length < 2) return null
  let best: { delta: number; centers: number[] } | null = null
  const consider = (target: number, a: number, b: number) => {
    const delta = target - dragC
    if (Math.abs(delta) > tol) return
    if (!best || Math.abs(delta) < Math.abs(best.delta)) best = { delta, centers: [a, b, target].sort((x, y) => x - y) }
  }
  for (let i = 0; i < centers.length - 1; i++) {
    const a = centers[i], b = centers[i + 1]
    const gap = b - a
    if (gap < 20) continue
    consider(b + gap, a, b)        // dragged sits after the pair
    consider(a - gap, a, b)        // dragged sits before the pair
    if (dragC > a && dragC < b) consider((a + b) / 2, a, b) // dragged centred between
  }
  return best
}

/**
 * Main entry: compute the magnetic correction + guides for a dragged box.
 * `tol` is in flow px (callers divide a screen tolerance by zoom).
 */
export function computeSmartSnap(drag: GuideBox, others: GuideBox[], tol: number): SmartSnapResult {
  if (!others.length) return NO_SNAP
  const ox = others.map((b) => ({ box: b, targets: xTargets(b) }))
  const oy = others.map((b) => ({ box: b, targets: yTargets(b) }))

  const xSnap = bestAxisSnap(xTargets(drag), ox, tol)
  const ySnap = bestAxisSnap(yTargets(drag), oy, tol)

  let dx = xSnap?.delta ?? 0
  let dy = ySnap?.delta ?? 0
  const v: GuideLine[] = []
  const h: GuideLine[] = []
  const gaps: GapHint[] = []

  if (xSnap) {
    const ys = [cy(drag) + dy, ...xSnap.matched.map(cy)]
    v.push({ coord: xSnap.coord, from: Math.min(...ys) - 48, to: Math.max(...ys) + 48 })
  }
  if (ySnap) {
    const xs = [cx(drag) + dx, ...ySnap.matched.map(cx)]
    h.push({ coord: ySnap.coord, from: Math.min(...xs) - 48, to: Math.max(...xs) + 48 })
  }

  // Equal-spacing pass (devices in the same row / column). Alignment wins on
  // its axis; spacing fills the other or refines an unsnapped axis.
  const ROW_TOL = 40
  const dCy = cy(drag) + dy
  const dCx = cx(drag) + dx
  if (!xSnap) {
    const rowMates = others.filter((b) => b.kind === 'device' && Math.abs(cy(b) - dCy) <= ROW_TOL && b.id !== drag.id)
    const g = equalGapSnap(cx(drag), rowMates, 'x', tol)
    if (g) {
      dx = g.delta
      const yLine = dCy - DISC_CY - 14 // just above the row of discs
      for (let i = 0; i < g.centers.length - 1; i++) gaps.push({ x1: g.centers[i], y1: yLine, x2: g.centers[i + 1], y2: yLine })
    }
  }
  if (!ySnap) {
    const colMates = others.filter((b) => b.kind === 'device' && Math.abs(cx(b) - dCx) <= ROW_TOL && b.id !== drag.id)
    const g = equalGapSnap(cy(drag), colMates, 'y', tol)
    if (g) {
      dy = g.delta
      const xLine = dCx - DISC_CX - 14 // just left of the column
      for (let i = 0; i < g.centers.length - 1; i++) gaps.push({ x1: xLine, y1: g.centers[i], x2: xLine, y2: g.centers[i + 1] })
    }
  }

  if (!dx && !dy && !v.length && !h.length && !gaps.length) return NO_SNAP
  return { dx, dy, v, h, gaps }
}

/* ── One-click layout tidy ──────────────────────────────────────────────────
 * Clusters nearly-aligned devices into exact rows and columns, then evens out
 * the horizontal gaps inside each row. Conservative: it only straightens what
 * the admin already roughly arranged; it never re-orders nodes. */

const CLUSTER_TOL = 36 // px: "roughly aligned" threshold
const FINE_GRID = 20   // px: snap row/column coordinates to this

export function tidyLayout(nodes: Node[]): Map<string, { x: number; y: number }> {
  const boxes = nodes.filter((n) => n.type !== 'shape').map(nodeToGuideBox)
  const pos = new Map(boxes.map((b) => [b.id, { x: b.x, y: b.y }]))
  if (boxes.length < 2) return new Map()

  // 1. Row clustering: nodes whose centres are within CLUSTER_TOL share a Y.
  const byY = [...boxes].sort((a, b) => cy(a) - cy(b))
  const rows: GuideBox[][] = []
  for (const b of byY) {
    const row = rows[rows.length - 1]
    if (row && Math.abs(cy(b) - cy(row[0])) <= CLUSTER_TOL) row.push(b)
    else rows.push([b])
  }
  for (const row of rows) {
    const meanCy = row.reduce((s, b) => s + cy(b), 0) / row.length
    const yCenter = snap(meanCy, FINE_GRID)
    for (const b of row) pos.get(b.id)!.y = yCenter - DISC_CY
  }

  // 2. Column clustering on X (same idea).
  const byX = [...boxes].sort((a, b) => cx(a) - cx(b))
  const cols: GuideBox[][] = []
  for (const b of byX) {
    const col = cols[cols.length - 1]
    if (col && Math.abs(cx(b) - cx(col[0])) <= CLUSTER_TOL) col.push(b)
    else cols.push([b])
  }
  for (const col of cols) {
    const meanCx = col.reduce((s, b) => s + cx(b), 0) / col.length
    const xCenter = snap(meanCx, FINE_GRID)
    for (const b of col) pos.get(b.id)!.x = xCenter - DISC_CX
  }

  // 3. Even out spacing inside each row of ≥3 (median gap, keeps order).
  for (const row of rows) {
    if (row.length < 3) continue
    const sorted = [...row].sort((a, b) => pos.get(a.id)!.x - pos.get(b.id)!.x)
    const centers = sorted.map((b) => pos.get(b.id)!.x + DISC_CX)
    const gapsArr = centers.slice(1).map((c, i) => c - centers[i])
    const sortedGaps = [...gapsArr].sort((a, b) => a - b)
    const median = sortedGaps[Math.floor(sortedGaps.length / 2)]
    const uniform = Math.max(150, snap(median, FINE_GRID))
    sorted.forEach((b, i) => { pos.get(b.id)!.x = centers[0] + uniform * i - DISC_CX })
  }

  // Only report actual moves.
  const out = new Map<string, { x: number; y: number }>()
  for (const b of boxes) {
    const p = pos.get(b.id)!
    if (Math.abs(p.x - b.x) > 0.5 || Math.abs(p.y - b.y) > 0.5) out.set(b.id, { x: Math.round(p.x), y: Math.round(p.y) })
  }
  return out
}
