import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'

export type MenuItem =
  | { type: 'item'; label: string; icon?: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }
  | { type: 'divider' }
  | { type: 'header'; label: string }

export type ContextMenuState = {
  x: number
  y: number
  items: MenuItem[]
} | null

export function ContextMenu({ state, onClose }: { state: ContextMenuState; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!state) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [state, onClose])

  if (!state) return null

  // Keep the menu inside the viewport.
  const x = Math.min(state.x, window.innerWidth - 220)
  const y = Math.min(state.y, window.innerHeight - 40 - state.items.length * 30)

  return (
    <div
      ref={ref}
      className="fixed z-[80] min-w-[190px] overflow-hidden rounded-lg border border-border bg-surface/95 py-1 text-sm shadow-xl backdrop-blur"
      style={{ left: x, top: y }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {state.items.map((it, i) => {
        if (it.type === 'divider') return <div key={i} className="my-1 h-px bg-border" />
        if (it.type === 'header') return <div key={i} className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted">{it.label}</div>
        return (
          <button
            key={i}
            type="button"
            disabled={it.disabled}
            onClick={() => { if (!it.disabled) { it.onClick(); onClose() } }}
            className={cn(
              'flex w-full items-center gap-2.5 px-3 py-1.5 text-left transition',
              it.disabled ? 'cursor-not-allowed text-muted/50' : it.danger ? 'text-danger hover:bg-danger/10' : 'text-text hover:bg-primary/10',
            )}
          >
            {it.icon && <span className="flex h-4 w-4 items-center justify-center text-muted">{it.icon}</span>}
            {it.label}
          </button>
        )
      })}
    </div>
  )
}
