import { useRef } from 'react'

export type GItem = { id: string; x: number; y: number; w: number; h: number }
type Handle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'

/* Bounding box + 8 handles around the current multi-selection. Dragging a
 * handle scales the SPACING between selected items (their positions, not their
 * sizes) about the opposite edge/corner — like a group transform in Figma —
 * so admins can spread out or tighten a cluster and keep alignment. */
export function GroupResizer({ items, transform, screenToFlow, onApply }: {
  items: GItem[]
  transform: [number, number, number]
  screenToFlow: (p: { x: number; y: number }) => { x: number; y: number }
  onApply: (moves: { id: string; x: number; y: number }[], commit: boolean) => void
}) {
  const dragging = useRef(false)
  if (items.length < 2) return null

  const [tx, ty, zoom] = transform
  const minX = Math.min(...items.map((i) => i.x))
  const minY = Math.min(...items.map((i) => i.y))
  const maxX = Math.max(...items.map((i) => i.x + i.w))
  const maxY = Math.max(...items.map((i) => i.y + i.h))
  const left = minX * zoom + tx
  const top = minY * zoom + ty
  const width = (maxX - minX) * zoom
  const height = (maxY - minY) * zoom

  const start = (handle: Handle) => (e: React.PointerEvent) => {
    e.preventDefault(); e.stopPropagation()
    dragging.current = true
    const orig = items.map((i) => ({ ...i }))
    const ob = { minX, minY, maxX, maxY }
    // Anchor = the edge/corner that stays put (opposite the dragged handle).
    const anchorX = handle.includes('w') ? ob.maxX : ob.minX
    const anchorY = handle.includes('n') ? ob.maxY : ob.minY
    const origW = Math.max(1, ob.maxX - ob.minX)
    const origH = Math.max(1, ob.maxY - ob.minY)
    const lockX = handle === 'n' || handle === 's'
    const lockY = handle === 'e' || handle === 'w'

    const compute = (ev: PointerEvent) => {
      const p = screenToFlow({ x: ev.clientX, y: ev.clientY })
      let sx = lockX ? 1 : Math.max(0.2, Math.abs(p.x - anchorX) / origW)
      let sy = lockY ? 1 : Math.max(0.2, Math.abs(p.y - anchorY) / origH)
      return orig.map((o) => ({ id: o.id, x: anchorX + (o.x - anchorX) * sx, y: anchorY + (o.y - anchorY) * sy }))
    }
    const move = (ev: PointerEvent) => onApply(compute(ev), false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      dragging.current = false
      onApply(compute(ev), true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const HANDLES: { h: Handle; cls: string; cursor: string }[] = [
    { h: 'nw', cls: 'left-0 top-0 -translate-x-1/2 -translate-y-1/2', cursor: 'nwse-resize' },
    { h: 'n', cls: 'left-1/2 top-0 -translate-x-1/2 -translate-y-1/2', cursor: 'ns-resize' },
    { h: 'ne', cls: 'right-0 top-0 translate-x-1/2 -translate-y-1/2', cursor: 'nesw-resize' },
    { h: 'e', cls: 'right-0 top-1/2 translate-x-1/2 -translate-y-1/2', cursor: 'ew-resize' },
    { h: 'se', cls: 'right-0 bottom-0 translate-x-1/2 translate-y-1/2', cursor: 'nwse-resize' },
    { h: 's', cls: 'left-1/2 bottom-0 -translate-x-1/2 translate-y-1/2', cursor: 'ns-resize' },
    { h: 'sw', cls: 'left-0 bottom-0 -translate-x-1/2 translate-y-1/2', cursor: 'nesw-resize' },
    { h: 'w', cls: 'left-0 top-1/2 -translate-x-1/2 -translate-y-1/2', cursor: 'ew-resize' },
  ]

  return (
    <div className="pointer-events-none absolute z-30" style={{ left, top, width, height }}>
      <div className="absolute inset-0 rounded border border-dashed border-primary/70" />
      {HANDLES.map(({ h, cls, cursor }) => (
        <div
          key={h}
          onPointerDown={start(h)}
          className={`pointer-events-auto absolute h-2.5 w-2.5 rounded-sm border border-surface bg-primary shadow ${cls}`}
          style={{ cursor }}
        />
      ))}
    </div>
  )
}
