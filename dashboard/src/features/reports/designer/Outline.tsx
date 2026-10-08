import {
  forwardRef, Fragment, useEffect, useId, useImperativeHandle, useLayoutEffect, useRef, useState, type DragEvent,
  type KeyboardEvent, type ReactNode,
} from 'react'
import {
  AlertCircle, ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpToLine, Camera, CopyPlus, GripVertical, LockKeyhole,
  MoreHorizontal, PenLine, Trash2, Undo2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import {
  dropIndex, duplicateEntry, entryTitle, insertEntry, MAX_SECTIONS, moveEntry, newUid, optionSummary, positionText,
  removeEntry, type DesignerEntry, type EntryProblems, type SectionIndex,
} from './model'
import { DND_ENTRY, DND_SECTION, Hint } from './ui'

export interface OutlineProps {
  entries: DesignerEntry[]
  sections: SectionIndex
  problems: Record<string, EntryProblems>
  selectedUid: string | null
  /** `open` is true when the user clicked a card (open the inspector drawer on small screens). */
  onSelect: (uid: string | null, opts?: { open?: boolean }) => void
  onEntriesChange: (next: DesignerEntry[]) => void
  /** A library section dropped into the outline at insertion slot `at`. */
  onDropSection: (sectionId: string, at: number) => void
  announce: (message: string) => void
  /** Narrow column: actions collapse into a menu. */
  dense?: boolean
  /** Buttons for the outline header (add sections, view switch). */
  toolbar?: ReactNode
  /** Shown instead of the list when there are no entries. */
  empty: ReactNode
  /** DOM id for the outline heading (focus target). */
  headingId: string
  className?: string
}

type Grab = { uid: string; origin: number }
/** Entry actions the inspector triggers, so they behave exactly like the outline's own buttons. */
export interface OutlineHandle {
  remove: (uid: string, opts?: { focus?: boolean }) => void
  duplicate: (uid: string, opts?: { focus?: boolean }) => void
  /** Focus an entry's card (or the outline heading when it is gone). */
  focusEntry: (uid: string | null) => void
}

type FocusEl = 'grip' | 'title' | 'up' | 'down' | 'more'
type FocusTarget = { uid: string; el: FocusEl } | { heading: true }

/**
 * The ordered list of sections in the template. Reorder with the grip
 * (drag, or Space then arrow keys, Enter to drop and Escape to cancel) or
 * the move buttons; every change is announced in a polite live region.
 */
export const Outline = forwardRef<OutlineHandle, OutlineProps>(function Outline({
  entries, sections, problems, selectedUid, onSelect, onEntriesChange, onDropSection, announce, dense, toolbar, empty,
  headingId, className,
}, ref) {
  const instrId = useId()
  const [grab, setGrab] = useState<Grab | null>(null)
  const [dragUid, setDragUid] = useState<string | null>(null)
  const [dragKind, setDragKind] = useState<'entry' | 'section' | null>(null)
  const [slot, setSlot] = useState<number | null>(null)
  const [undo, setUndo] = useState<{ entry: DesignerEntry; index: number } | null>(null)
  const listRef = useRef<HTMLOListElement>(null)
  const refs = useRef(new Map<string, HTMLElement>())
  const focusNext = useRef<FocusTarget | null>(null)
  const n = entries.length
  const full = n >= MAX_SECTIONS

  const setRef = (key: string) => (el: HTMLElement | null) => {
    if (el) refs.current.set(key, el)
    else refs.current.delete(key)
  }

  // Keep focus on the moved item (React may re-insert its DOM node).
  useLayoutEffect(() => {
    const f = focusNext.current
    if (!f) return
    focusNext.current = null
    if ('heading' in f) {
      document.getElementById(headingId)?.focus()
      return
    }
    let el = refs.current.get(`${f.uid}:${f.el}`)
    if (!el || (el as HTMLButtonElement).disabled) el = refs.current.get(`${f.uid}:grip`)
    el?.focus({ preventScroll: true })
    el?.closest('li')?.scrollIntoView({ block: 'nearest' })
  })

  // The undo bar disappears after a while.
  useEffect(() => {
    if (!undo) return
    const t = window.setTimeout(() => setUndo(null), 10_000)
    return () => window.clearTimeout(t)
  }, [undo])

  const titleOf = (e: DesignerEntry) => entryTitle(e, sections[e.id])

  const move = (index: number, to: number, focus: FocusEl, verb?: string) => {
    const e = entries[index]
    if (!e) return
    const target = Math.max(0, Math.min(n - 1, to))
    if (target === index) {
      announce(index === 0 ? `${titleOf(e)} is already first` : `${titleOf(e)} is already last`)
      return
    }
    focusNext.current = { uid: e.uid, el: focus }
    onEntriesChange(moveEntry(entries, index, target))
    announce(verb ? `${verb}. ${positionText(titleOf(e), target, n)}` : positionText(titleOf(e), target, n))
  }

  const remove = (index: number, focus = true) => {
    const e = entries[index]
    if (!e) return
    const neighbour = entries[index + 1] ?? entries[index - 1] ?? null
    onEntriesChange(removeEntry(entries, index))
    if (selectedUid === e.uid) onSelect(neighbour?.uid ?? null)
    if (focus) focusNext.current = neighbour ? { uid: neighbour.uid, el: 'title' } : { heading: true }
    setUndo({ entry: e, index })
    announce(`Removed ${titleOf(e)}. ${n - 1} section${n - 1 === 1 ? '' : 's'} left.`)
  }

  const duplicate = (index: number, focus = true) => {
    const e = entries[index]
    if (!e || full) return
    const uid = newUid()
    onEntriesChange(duplicateEntry(entries, index, uid))
    onSelect(uid)
    if (focus) focusNext.current = { uid, el: 'title' }
    announce(`Duplicated ${titleOf(e)}. The copy is ${positionText(titleOf(e), index + 1, n + 1)}`)
  }

  const restore = () => {
    if (!undo || full) return
    const at = Math.min(undo.index, n)
    onEntriesChange(insertEntry(entries, undo.entry, at))
    onSelect(undo.entry.uid)
    focusNext.current = { uid: undo.entry.uid, el: 'title' }
    announce(`Restored ${titleOf(undo.entry)}. ${positionText(titleOf(undo.entry), at, n + 1)}`)
    setUndo(null)
  }

  useImperativeHandle(ref, () => ({
    remove: (uid, opts) => remove(entries.findIndex((x) => x.uid === uid), opts?.focus ?? true),
    duplicate: (uid, opts) => duplicate(entries.findIndex((x) => x.uid === uid), opts?.focus ?? true),
    focusEntry: (uid) => {
      const el = uid ? refs.current.get(`${uid}:title`) : null
      if (el) {
        el.focus({ preventScroll: true })
        el.closest('li')?.scrollIntoView({ block: 'nearest' })
      } else {
        document.getElementById(headingId)?.focus()
      }
    },
  }))

  /* ── Keyboard reordering on the grip ─────────────────────────────── */
  const onGripKey = (ev: KeyboardEvent<HTMLElement>, index: number) => {
    const e = entries[index]
    const grabbed = grab?.uid === e.uid
    const title = titleOf(e)
    switch (ev.key) {
      case ' ':
      case 'Enter':
        ev.preventDefault()
        if (grabbed) {
          setGrab(null)
          announce(`Dropped. ${positionText(title, index, n)}`)
        } else {
          setGrab({ uid: e.uid, origin: index })
          announce(`Picked up ${title}, position ${index + 1} of ${n}. Use the up and down arrow keys to move it, Space or Enter to drop it, Escape to cancel.`)
        }
        return
      case 'ArrowUp':
      case 'ArrowDown': {
        ev.preventDefault()
        const delta = ev.key === 'ArrowUp' ? -1 : 1
        if (grabbed) {
          move(index, index + delta, 'grip')
        } else {
          const next = entries[index + delta]
          if (next) refs.current.get(`${next.uid}:grip`)?.focus()
        }
        return
      }
      case 'Home':
      case 'End':
        if (!grabbed) return
        ev.preventDefault()
        move(index, ev.key === 'Home' ? 0 : n - 1, 'grip')
        return
      case 'Escape':
        if (!grabbed) return
        ev.preventDefault()
        ev.stopPropagation()
        if (grab && grab.origin !== index) {
          focusNext.current = { uid: e.uid, el: 'grip' }
          onEntriesChange(moveEntry(entries, index, grab.origin))
        }
        setGrab(null)
        announce(`Cancelled. ${title} is back at position ${(grab?.origin ?? index) + 1} of ${n}.`)
        return
    }
  }

  const onGripBlur = (uid: string) => {
    // Focus may blink while React moves the node; drop only when it really left.
    window.setTimeout(() => {
      setGrab((g) => (g && g.uid === uid && document.activeElement !== refs.current.get(`${uid}:grip`) ? null : g))
    }, 0)
  }

  /* ── Pointer drag and drop ────────────────────────────────────────── */
  const accepts = (ev: DragEvent) => {
    const types = Array.from(ev.dataTransfer.types)
    if (types.includes(DND_ENTRY)) return 'entry'
    if (types.includes(DND_SECTION)) return full ? null : 'section'
    return null
  }

  const slotAt = (clientY: number) => {
    const items = listRef.current?.querySelectorAll<HTMLElement>('[data-entry-uid]') ?? []
    for (let i = 0; i < items.length; i++) {
      const r = items[i].getBoundingClientRect()
      if (clientY < r.top + r.height / 2) return i
    }
    return items.length
  }

  const onDragOver = (ev: DragEvent<HTMLDivElement>) => {
    const kind = accepts(ev)
    if (!kind) return
    ev.preventDefault()
    ev.dataTransfer.dropEffect = kind === 'entry' ? 'move' : 'copy'
    if (dragKind !== kind) setDragKind(kind)
    const s = slotAt(ev.clientY)
    if (s !== slot) setSlot(s)
  }

  const clearDrag = () => {
    setSlot(null)
    setDragUid(null)
    setDragKind(null)
  }

  const onDrop = (ev: DragEvent<HTMLDivElement>) => {
    const kind = accepts(ev)
    if (!kind) return
    ev.preventDefault()
    const at = slot ?? slotAt(ev.clientY)
    if (kind === 'entry') {
      const uid = ev.dataTransfer.getData(DND_ENTRY)
      const from = entries.findIndex((x) => x.uid === uid)
      if (from >= 0) {
        const to = dropIndex(from, at)
        if (to !== from) {
          onEntriesChange(moveEntry(entries, from, to))
          announce(`Moved ${titleOf(entries[from])}. ${positionText(titleOf(entries[from]), to, n)}`)
        }
      }
    } else {
      const sid = ev.dataTransfer.getData(DND_SECTION)
      if (sid) onDropSection(sid, at)
    }
    clearDrag()
  }

  const dragFrom = dragUid ? entries.findIndex((x) => x.uid === dragUid) : -1
  const showSlot = (i: number) =>
    slot === i && dragKind !== null && !(dragKind === 'entry' && dragFrom >= 0 && (i === dragFrom || i === dragFrom + 1))

  const indicator = (i: number) =>
    showSlot(i) ? <li aria-hidden className="-my-[3px] h-[2px] rounded-full bg-primary shadow-[0_0_0_2px_rgb(var(--primary)/0.2)]" /> : null

  return (
    <section aria-labelledby={headingId} className={cn('flex min-h-0 flex-col', className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 border-b border-border px-3 py-2">
        <h2 id={headingId} tabIndex={-1} className="text-sm font-semibold text-text focus:outline-none">
          Outline
        </h2>
        <span className={cn('text-xs tabular-nums', full ? 'font-medium text-warning-fg' : 'text-muted')}>
          {n}/{MAX_SECTIONS} sections
        </span>
        <div className="ml-auto flex items-center gap-1.5">{toolbar}</div>
      </div>

      <p id={instrId} className="sr-only">
        To reorder, press Space or Enter on a grip handle, move with the up and down arrow keys, then press Space or Enter to drop
        or Escape to cancel. Each section also has move buttons.
      </p>

      {undo && (
        <div role="status" className="mx-3 mt-2 flex items-center gap-2 rounded-md border border-border bg-surface2/70 px-2.5 py-1.5 text-xs text-text2">
          <span className="min-w-0 flex-1 truncate">Removed “{titleOf(undo.entry)}”</span>
          <button
            type="button"
            onClick={restore}
            disabled={full}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-primary hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:opacity-50"
          >
            <Undo2 className="h-3.5 w-3.5" aria-hidden /> Undo
          </button>
        </div>
      )}

      <div
        className="min-h-0 flex-1 overflow-y-auto p-3"
        onDragOver={onDragOver}
        onDragLeave={(ev) => {
          if (!ev.currentTarget.contains(ev.relatedTarget as Node | null)) setSlot(null)
        }}
        onDrop={onDrop}
      >
        {n === 0 ? (
          <div className={cn('h-full rounded-xl transition-colors', dragKind === 'section' && 'bg-primary/5 outline-dashed outline-2 outline-primary/40')}>
            {empty}
          </div>
        ) : (
          <ol ref={listRef} aria-label="Sections in this template" className="space-y-2">
            {entries.map((e, i) => {
              const def = sections[e.id]
              const p = problems[e.uid]
              const issueCount = p ? Object.keys(p.options).length + (p.title ? 1 : 0) + (p.unknown || p.restricted ? 1 : 0) : 0
              const selected = e.uid === selectedUid
              const grabbed = grab?.uid === e.uid
              const title = titleOf(e)
              const summary = def ? optionSummary(def, e.options, dense ? 2 : 3) : ''
              const renamed = !!e.title.trim() && !!def && e.title.trim() !== def.title
              return (
                <Fragment key={e.uid}>
                {indicator(i)}
                <li
                  data-entry-uid={e.uid}
                  className={cn(
                    'group/card relative flex items-stretch rounded-lg border bg-surface transition-[border-color,box-shadow,opacity]',
                    selected ? 'border-primary/60 shadow-[0_0_0_1px_rgb(var(--primary)/0.35)]' : 'border-border hover:border-border-strong',
                    issueCount > 0 && !selected && 'border-danger/50',
                    grabbed && 'z-10 border-primary shadow-elevated ring-2 ring-primary/50',
                    dragUid === e.uid && 'opacity-40',
                  )}
                >
                  <div
                    ref={setRef(`${e.uid}:grip`)}
                    role="button"
                    tabIndex={0}
                    aria-roledescription="sortable handle"
                    aria-label={`Reorder ${title}, position ${i + 1} of ${n}`}
                    aria-describedby={instrId}
                    aria-pressed={grabbed}
                    draggable
                    onDragStart={(ev) => {
                      ev.dataTransfer.setData(DND_ENTRY, e.uid)
                      ev.dataTransfer.setData('text/plain', title)
                      ev.dataTransfer.effectAllowed = 'move'
                      const card = ev.currentTarget.closest('li')
                      if (card) ev.dataTransfer.setDragImage(card, 16, 20)
                      setDragUid(e.uid)
                      setDragKind('entry')
                      setGrab(null)
                    }}
                    onDragEnd={clearDrag}
                    onKeyDown={(ev) => onGripKey(ev, i)}
                    onBlur={() => onGripBlur(e.uid)}
                    className={cn(
                      'flex w-7 shrink-0 cursor-grab touch-none items-center justify-center rounded-l-lg text-muted transition-colors active:cursor-grabbing',
                      'hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary',
                      grabbed && 'bg-primary/10 text-primary',
                    )}
                  >
                    <GripVertical className="h-4 w-4" aria-hidden />
                  </div>

                  <div className="flex min-w-0 flex-1 items-start gap-2.5 py-2 pl-1.5 pr-1.5">
                    <span
                      aria-hidden
                      className={cn(
                        'mt-px flex h-6 min-w-[1.5rem] shrink-0 items-center justify-center rounded-md px-1 text-[11px] font-bold tabular-nums',
                        selected ? 'bg-primary text-on-primary' : 'bg-primary/10 text-primary',
                      )}
                    >
                      {String(i + 1).padStart(2, '0')}
                    </span>

                    <button
                      ref={setRef(`${e.uid}:title`) as (el: HTMLButtonElement | null) => void}
                      type="button"
                      onClick={() => onSelect(e.uid, { open: true })}
                      aria-current={selected ? 'true' : undefined}
                      className="min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className={cn('truncate text-[13px] font-medium', def ? 'text-text' : 'text-danger-fg')}>{title}</span>
                        {renamed && (
                          <span className="inline-flex shrink-0 items-center text-muted" title={`Default title: ${def?.title}`}>
                            <PenLine className="h-3 w-3" aria-hidden />
                            <span className="sr-only">(renamed from {def?.title})</span>
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block truncate text-[11px] text-muted">
                        {!def ? 'This section is no longer available' : summary || (def.options.length ? '' : 'No options')}
                      </span>
                      {issueCount > 0 && (
                        <span className="mt-1 flex items-center gap-1 text-[11px] font-medium text-danger-fg">
                          <AlertCircle className="h-3 w-3 shrink-0" aria-hidden />
                          {p?.unknown || p?.restricted ? 'Remove this section to save' : `${issueCount} option${issueCount === 1 ? '' : 's'} to fix`}
                        </span>
                      )}
                    </button>

                    {def && (!def.available || def.semantics === 'snapshot') && (
                      <span className="mt-1 flex shrink-0 items-center gap-1">
                        {!def.available && (
                          <Hint content="Your role cannot see this section's data; it shows as restricted in your preview.">
                            <span className="text-warning-fg">
                              <LockKeyhole className="h-3.5 w-3.5" aria-hidden />
                              <span className="sr-only">Restricted for your role</span>
                            </span>
                          </Hint>
                        )}
                        {def.semantics === 'snapshot' && (
                          <Hint content="Snapshot: shows the state when the report runs, not the report period.">
                            <span className="text-info-fg">
                              <Camera className="h-3.5 w-3.5" aria-hidden />
                              <span className="sr-only">Snapshot</span>
                            </span>
                          </Hint>
                        )}
                      </span>
                    )}

                    <EntryActions
                      dense={dense}
                      title={title}
                      first={i === 0}
                      last={i === n - 1}
                      full={full}
                      setRef={(k) => setRef(`${e.uid}:${k}`)}
                      onUp={() => move(i, i - 1, 'up', 'Moved up')}
                      onDown={() => move(i, i + 1, 'down', 'Moved down')}
                      onTop={() => move(i, 0, 'more', 'Moved to the top')}
                      onBottom={() => move(i, n - 1, 'more', 'Moved to the bottom')}
                      onDuplicate={() => duplicate(i)}
                      onRemove={() => remove(i)}
                    />
                  </div>
                </li>
                </Fragment>
              )
            })}
            {indicator(n)}
          </ol>
        )}
      </div>
    </section>
  )
})

function EntryActions({
  dense, title, first, last, full, setRef, onUp, onDown, onTop, onBottom, onDuplicate, onRemove,
}: {
  dense?: boolean
  title: string
  first: boolean
  last: boolean
  full: boolean
  setRef: (key: 'up' | 'down' | 'more') => (el: HTMLElement | null) => void
  onUp: () => void
  onDown: () => void
  onTop: () => void
  onBottom: () => void
  onDuplicate: () => void
  onRemove: () => void
}) {
  const icon = 'flex h-7 w-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:pointer-events-none disabled:opacity-30'
  const menu = (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button ref={setRef('more') as (el: HTMLButtonElement | null) => void} type="button" className={icon} aria-label={`More actions for ${title}`}>
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="min-w-[180px]">
        {dense && (
          <>
            <DropdownMenuItem disabled={first} onSelect={onUp}><ArrowUp aria-hidden /> Move up</DropdownMenuItem>
            <DropdownMenuItem disabled={last} onSelect={onDown}><ArrowDown aria-hidden /> Move down</DropdownMenuItem>
          </>
        )}
        <DropdownMenuItem disabled={first} onSelect={onTop}><ArrowUpToLine aria-hidden /> Move to top</DropdownMenuItem>
        <DropdownMenuItem disabled={last} onSelect={onBottom}><ArrowDownToLine aria-hidden /> Move to bottom</DropdownMenuItem>
        {dense && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={full} onSelect={onDuplicate}><CopyPlus aria-hidden /> Duplicate</DropdownMenuItem>
            <DropdownMenuItem destructive onSelect={onRemove}><Trash2 aria-hidden /> Remove</DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
  if (dense) return <div className="flex shrink-0 items-center">{menu}</div>
  return (
    <div className="flex shrink-0 items-center opacity-80 transition-opacity group-hover/card:opacity-100 group-focus-within/card:opacity-100">
      <button ref={setRef('up') as (el: HTMLButtonElement | null) => void} type="button" className={icon} disabled={first} onClick={onUp} aria-label={`Move ${title} up`}>
        <ArrowUp className="h-3.5 w-3.5" aria-hidden />
      </button>
      <button ref={setRef('down') as (el: HTMLButtonElement | null) => void} type="button" className={icon} disabled={last} onClick={onDown} aria-label={`Move ${title} down`}>
        <ArrowDown className="h-3.5 w-3.5" aria-hidden />
      </button>
      <button type="button" className={icon} disabled={full} onClick={onDuplicate} aria-label={`Duplicate ${title}`} title={full ? `At most ${MAX_SECTIONS} sections` : 'Duplicate'}>
        <CopyPlus className="h-3.5 w-3.5" aria-hidden />
      </button>
      <button type="button" className={cn(icon, 'hover:bg-danger/10 hover:text-danger-fg')} onClick={onRemove} aria-label={`Remove ${title}`} title="Remove">
        <Trash2 className="h-3.5 w-3.5" aria-hidden />
      </button>
      {menu}
    </div>
  )
}
