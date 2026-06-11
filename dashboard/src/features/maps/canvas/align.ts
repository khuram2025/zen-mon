/* Pure alignment / distribution helpers operating on React Flow node geometry.
 * Each takes the current selection and returns a Map of id -> new {x,y}
 * (React Flow top-left position). Geometry uses each node's measured size so
 * alignment is on the visual box; callers persist via disc-centre conversion. */

import type { Node } from '@xyflow/react'

export type AlignOp =
  | 'left' | 'right' | 'top' | 'bottom'
  | 'center-h' | 'center-v'
  | 'distribute-h' | 'distribute-v'

type Box = { id: string; x: number; y: number; w: number; h: number }

function boxes(nodes: Node[]): Box[] {
  return nodes.map((n) => ({
    id: n.id,
    x: n.position.x,
    y: n.position.y,
    w: n.measured?.width ?? (n.width as number) ?? 128,
    h: n.measured?.height ?? (n.height as number) ?? 96,
  }))
}

export function computeAlign(nodes: Node[], op: AlignOp): Map<string, { x: number; y: number }> {
  const out = new Map<string, { x: number; y: number }>()
  const bs = boxes(nodes)
  if (bs.length < 2) return out

  const minX = Math.min(...bs.map((b) => b.x))
  const maxR = Math.max(...bs.map((b) => b.x + b.w))
  const minY = Math.min(...bs.map((b) => b.y))
  const maxB = Math.max(...bs.map((b) => b.y + b.h))
  const cx = (minX + maxR) / 2
  const cy = (minY + maxB) / 2

  for (const b of bs) {
    let { x, y } = b
    switch (op) {
      case 'left': x = minX; break
      case 'right': x = maxR - b.w; break
      case 'top': y = minY; break
      case 'bottom': y = maxB - b.h; break
      case 'center-h': x = cx - b.w / 2; break
      case 'center-v': y = cy - b.h / 2; break
      default: break
    }
    out.set(b.id, { x, y })
  }

  if (op === 'distribute-h' || op === 'distribute-v') {
    const horiz = op === 'distribute-h'
    const sorted = [...bs].sort((a, b) => (horiz ? a.x - b.x : a.y - b.y))
    if (sorted.length >= 3) {
      const first = sorted[0]
      const last = sorted[sorted.length - 1]
      const start = horiz ? first.x + first.w / 2 : first.y + first.h / 2
      const end = horiz ? last.x + last.w / 2 : last.y + last.h / 2
      const step = (end - start) / (sorted.length - 1)
      sorted.forEach((b, i) => {
        const center = start + step * i
        if (horiz) out.set(b.id, { x: center - b.w / 2, y: b.y })
        else out.set(b.id, { x: b.x, y: center - b.h / 2 })
      })
    }
  }
  return out
}

/** Snap a value to the nearest grid multiple. */
export function snap(v: number, grid: number): number {
  return Math.round(v / grid) * grid
}
