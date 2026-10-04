/**
 * Section tabs with complete ARIA tab semantics (UI 5, UI 6): roving
 * tabindex, arrow/Home/End navigation, tab ↔ panel relationships, and the
 * active tab scrolled into view on narrow screens with edge fades that show
 * more tabs exist off-screen.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export type DetailTab<K extends string> = { key: K; label: string; Icon: LucideIcon; count?: number }

export const tabId = (key: string) => `svc-tab-${key}`
export const panelId = (key: string) => `svc-panel-${key}`

export function DetailTabs<K extends string>({ tabs, active, onChange, label }: {
  tabs: ReadonlyArray<DetailTab<K>>
  active: K
  onChange: (k: K) => void
  label: string
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const refs = useRef(new Map<K, HTMLButtonElement>())
  const [fade, setFade] = useState({ left: false, right: false })

  const updateFade = useCallback(() => {
    const el = listRef.current
    if (!el) return
    setFade({ left: el.scrollLeft > 4, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 4 })
  }, [])

  useLayoutEffect(() => {
    // Horizontal only: scrollIntoView would also scroll the page vertically.
    const list = listRef.current
    const tab = refs.current.get(active)
    if (list && tab) {
      const left = tab.offsetLeft - list.offsetLeft
      const right = left + tab.offsetWidth
      if (left < list.scrollLeft + 24) list.scrollLeft = Math.max(0, left - 24)
      else if (right > list.scrollLeft + list.clientWidth - 24) list.scrollLeft = right - list.clientWidth + 24
    }
    updateFade()
    // Counts arriving later change tab widths; re-align then too.
  }, [active, updateFade, tabs.map((t) => t.count ?? 0).join(',')])

  useEffect(() => {
    const el = listRef.current
    if (!el) return
    el.addEventListener('scroll', updateFade, { passive: true })
    window.addEventListener('resize', updateFade)
    return () => {
      el.removeEventListener('scroll', updateFade)
      window.removeEventListener('resize', updateFade)
    }
  }, [updateFade])

  const onKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = tabs.findIndex((t) => t.key === active)
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next < 0) return
    e.preventDefault()
    const k = tabs[next].key
    onChange(k)
    refs.current.get(k)?.focus()
  }

  return (
    <div className="sticky top-0 z-20 -mx-1 border-b border-border bg-bg/95 backdrop-blur supports-[backdrop-filter]:bg-bg/80">
      <div className="relative">
        <div ref={listRef} role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto px-1 [scrollbar-width:none]">
          {tabs.map((t) => {
            const selected = t.key === active
            return (
              <button
                key={t.key}
                ref={(el) => { if (el) refs.current.set(t.key, el); else refs.current.delete(t.key) }}
                id={tabId(t.key)}
                role="tab"
                type="button"
                aria-selected={selected}
                aria-controls={panelId(t.key)}
                tabIndex={selected ? 0 : -1}
                onClick={() => onChange(t.key)}
                onKeyDown={onKey}
                className={cn(
                  'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/50',
                  selected ? 'border-primary text-primary' : 'border-transparent text-muted hover:border-border hover:text-text',
                )}
              >
                <t.Icon className="h-4 w-4" aria-hidden />
                {t.label}
                {(t.count ?? 0) > 0 && (
                  <span className="ml-0.5 rounded-full bg-surface3 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-text2">
                    {t.count}
                    <span className="sr-only"> items</span>
                  </span>
                )}
              </button>
            )
          })}
        </div>
        {fade.left && <div aria-hidden className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-bg to-transparent" />}
        {fade.right && <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-bg to-transparent" />}
      </div>
    </div>
  )
}

export function DetailTabPanel({ tab, children }: { tab: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={panelId(tab)} aria-labelledby={tabId(tab)} tabIndex={0} className="focus-visible:outline-none">
      {children}
    </div>
  )
}
