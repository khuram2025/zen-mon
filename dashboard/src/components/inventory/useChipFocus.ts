import { useCallback, useEffect, useRef } from 'react'

/**
 * A click that will navigate in THIS tab: the primary button with no modifier. Ctrl/Cmd/Shift/Alt-click and
 * middle-click open a new tab or window and leave this page as it is, so nothing that prepares for "the chip is
 * gone" (a focus move, an emptied search box) may run for them (review findings NF-05 and NF-06).
 */
export function isPlainClick(e: { button?: number; metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean; altKey?: boolean; defaultPrevented?: boolean }): boolean {
  return !e.defaultPrevented && (e.button ?? 0) === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
}

/**
 * How long an armed focus move waits for the chip list to change. A click whose navigation never changes the list
 * (cancelled, refused, superseded) must not leave the move armed for the next, unrelated change: typing in the
 * search box would otherwise have its focus pulled into a chip link.
 */
export const CHIP_FOCUS_WINDOW_MS = 2000

/** The element the focus goes to: the chip that took the removed one's place (or the last), else the fallback. */
export function chipFocusTarget<T>(index: number, links: ArrayLike<T> | undefined, fallback: T | null): T | null {
  if (index >= 0 && links && links.length > 0) return links[Math.min(index, links.length - 1)]
  return fallback
}

/**
 * Keyboard focus after a filter chip is removed or "Clear all" is pressed (review findings UI-08, FV1-N1).
 *
 * A removed chip unmounts the very element that had focus, and the browser drops focus to the document: a
 * screen reader then announces nothing and a sighted keyboard user sees no focus ring. The toolbar calls
 * `removed(index, event)` when a chip's remove link is activated and `cleared(event)` for "Clear all"; once the chip
 * list has changed, focus moves to the chip that took the removed one's place (or the last chip), and to `fallback`
 * (the search box) when no chip is left. The polite count line announces the new total by itself. A modified click
 * (new tab) arms nothing, and an armed move expires after {@link CHIP_FOCUS_WINDOW_MS}.
 *
 * `signature` is any string that changes whenever the chips do; `remove` is a selector for the remove links
 * inside `list`. Every Inventory list with filter chips (Assets, Changes, Software) uses this hook.
 */
export function useChipFocus(
  list: React.RefObject<HTMLElement | null>,
  fallback: React.RefObject<HTMLElement | null>,
  signature: string,
  remove = 'a[data-chip-remove]',
) {
  const want = useRef<number | null>(null)
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null)
  const disarm = useCallback(() => {
    want.current = null
    if (expiry.current !== null) clearTimeout(expiry.current)
    expiry.current = null
  }, [])
  const arm = useCallback((index: number) => {
    disarm()
    want.current = index
    expiry.current = setTimeout(disarm, CHIP_FOCUS_WINDOW_MS)
  }, [disarm])

  useEffect(() => {
    const index = want.current
    if (index === null) return
    disarm()
    const links = index >= 0 ? list.current?.querySelectorAll<HTMLElement>(remove) : undefined
    chipFocusTarget(index, links, fallback.current)?.focus()
  }, [signature, list, fallback, remove, disarm])
  useEffect(() => disarm, [disarm])
  return {
    /** A chip's remove link was activated; `index` is the chip's position in the list. */
    removed: useCallback((index: number, event?: Parameters<typeof isPlainClick>[0]) => {
      if (event && !isPlainClick(event)) return
      arm(index)
    }, [arm]),
    /** "Clear all" was activated: focus goes to the fallback. */
    cleared: useCallback((event?: Parameters<typeof isPlainClick>[0]) => {
      if (event && !isPlainClick(event)) return
      arm(-1)
    }, [arm]),
  }
}
