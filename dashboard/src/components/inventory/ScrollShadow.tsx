import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * A horizontally scrolling region that SAYS it scrolls (review finding UI-10): a fade on the edge that has more
 * content, plus a one-line hint under it while something is out of view. The browser's own scrollbar is thin and
 * hidden by some platforms, and a table whose last columns are cut off looks complete.
 *
 * The scroll container is focusable (`tabIndex=0`, labelled) so a keyboard user can scroll it with the arrow keys.
 */
export function ScrollShadow({
  children, label, hint = 'Scroll sideways to see more columns', className,
}: {
  children: ReactNode
  /** Accessible name of the scroll region. */
  label: string
  hint?: string
  className?: string
}) {
  const box = useRef<HTMLDivElement>(null)
  const [edges, setEdges] = useState({ left: false, right: false })
  const measure = useCallback(() => {
    const el = box.current
    if (!el) return
    const left = el.scrollLeft > 1
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1
    setEdges((cur) => (cur.left === left && cur.right === right ? cur : { left, right }))
  }, [])
  useEffect(() => {
    measure()
    const el = box.current
    if (!el) return
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(el)
    if (el.firstElementChild) ro?.observe(el.firstElementChild)
    return () => ro?.disconnect()
  }, [measure])
  return (
    <div className={cn('space-y-1', className)}>
      <div className="relative">
        <div
          ref={box}
          onScroll={measure}
          tabIndex={0}
          role="region"
          aria-label={label}
          className="overflow-x-auto rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          {children}
        </div>
        <span aria-hidden data-scroll-edge="left" className={cn('pointer-events-none absolute inset-y-0 left-0 w-6 bg-gradient-to-r from-surface to-transparent transition-opacity', edges.left ? 'opacity-100' : 'opacity-0')} />
        <span aria-hidden data-scroll-edge="right" className={cn('pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-surface to-transparent transition-opacity', edges.right ? 'opacity-100' : 'opacity-0')} />
      </div>
      {edges.right && <p className="text-xs text-muted" data-scroll-hint="">{hint}</p>}
    </div>
  )
}
