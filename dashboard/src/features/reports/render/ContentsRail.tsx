import { useEffect, useId, useState } from 'react'
import { ListTree, LockKeyhole } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { RenderSection } from '../types'
import { sectionAnchorId, sectionStatus, type SectionStatus } from './format'

/**
 * Scroll-spy: index of the section nearest the top of the viewport (below
 * `offsetPx` of sticky chrome). Uses IntersectionObserver; falls back to the
 * first section when it is unavailable.
 */
export function useScrollSpy(ids: string[], offsetPx = 120): number {
  const [active, setActive] = useState(0)
  const key = ids.join('|')
  useEffect(() => {
    if (!ids.length || typeof IntersectionObserver === 'undefined') return
    const visible = new Map<string, boolean>()
    const recompute = () => {
      // At the very bottom the last (often short) section can never reach the band.
      const doc = document.documentElement
      if (window.scrollY > 0 && window.innerHeight + window.scrollY >= doc.scrollHeight - 4) {
        setActive(ids.length - 1)
        return
      }
      const first = ids.findIndex((id) => visible.get(id))
      if (first >= 0) setActive(first)
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) visible.set(e.target.id, e.isIntersecting)
        recompute()
      },
      // A band from just below the sticky toolbar to 55% of the viewport.
      { rootMargin: `-${Math.round(offsetPx)}px 0px -45% 0px`, threshold: 0 },
    )
    for (const id of ids) {
      const el = document.getElementById(id)
      if (el) observer.observe(el)
    }
    let frame = 0
    const onScroll = () => {
      if (frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        recompute()
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      observer.disconnect()
      window.removeEventListener('scroll', onScroll)
      if (frame) cancelAnimationFrame(frame)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, offsetPx])
  return active
}

/** Move to a section: smooth scroll (unless reduced motion) and focus its landmark for screen readers. */
export function goToSection(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  el.focus({ preventScroll: true })
}

const DOT: Record<SectionStatus, string> = {
  ok: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  error: 'bg-warning',
  restricted: 'bg-muted2',
}

const STATUS_TEXT: Record<SectionStatus, string> = {
  ok: '',
  warning: 'has warnings',
  danger: 'has critical findings',
  error: 'could not be generated',
  restricted: 'restricted',
}

function StatusMark({ status }: { status: SectionStatus }) {
  if (status === 'restricted') return <LockKeyhole className="h-3 w-3 shrink-0 text-muted" aria-hidden />
  if (status === 'ok') return <span aria-hidden className="h-1.5 w-1.5 shrink-0" />
  return <span aria-hidden className={cn('h-2 w-2 shrink-0 rounded-full', DOT[status], status === 'error' && 'ring-2 ring-warning/30')} />
}

export interface ContentsProps {
  sections: RenderSection[]
  idPrefix: string
  active: number
  className?: string
}

/** Sticky contents rail (lg+): numbered sections, active section highlighted, status dots. */
export function ContentsRail({ sections, idPrefix, active, className }: ContentsProps) {
  if (sections.length < 2) return null
  return (
    <nav aria-label="Report contents" className={cn('text-xs', className)}>
      <p className="mb-2 flex items-center gap-1.5 px-2 text-2xs font-semibold uppercase tracking-wide text-muted">
        <ListTree className="h-3.5 w-3.5" aria-hidden /> Contents
      </p>
      <ol role="list" className="space-y-0.5 border-l border-border">
        {sections.map((s, i) => {
          const id = sectionAnchorId(idPrefix, i)
          const status = sectionStatus(s)
          const current = i === active
          return (
            <li key={id}>
              <a
                href={`#${id}`}
                aria-current={current ? 'location' : undefined}
                onClick={(e) => {
                  e.preventDefault()
                  goToSection(id)
                }}
                className={cn(
                  '-ml-px flex items-center gap-2 border-l-2 py-1.5 pl-3 pr-2 leading-snug transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40',
                  current ? 'border-primary bg-primary/5 font-medium text-text' : 'border-transparent text-text2 hover:border-border-strong hover:text-text',
                )}
              >
                <span className={cn('w-5 shrink-0 tabular-nums', current ? 'text-primary' : 'text-muted')}>
                  {String(i + 1).padStart(2, '0')}
                </span>
                <span className="min-w-0 flex-1 truncate" title={s.title}>{s.title}</span>
                <StatusMark status={status} />
                {STATUS_TEXT[status] && <span className="sr-only">({STATUS_TEXT[status]})</span>}
              </a>
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

/** Below lg: a native "Jump to section" select. */
export function ContentsSelect({ sections, idPrefix, active, className }: ContentsProps) {
  const id = useId()
  if (sections.length < 2) return null
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <label htmlFor={id} className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted">
        Jump to
      </label>
      <select
        id={id}
        value={active}
        onChange={(e) => goToSection(sectionAnchorId(idPrefix, Number(e.target.value)))}
        className="h-8 min-w-0 flex-1 rounded-md border border-border bg-surface px-2 text-xs text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        {sections.map((s, i) => {
          const status = sectionStatus(s)
          return (
            <option key={i} value={i}>
              {String(i + 1).padStart(2, '0')} · {s.title}{STATUS_TEXT[status] ? ` (${STATUS_TEXT[status]})` : ''}
            </option>
          )
        })}
      </select>
    </div>
  )
}
