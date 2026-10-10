import { useEffect, useRef, useSyncExternalStore } from 'react'
import { createSearchController, type SearchController } from './searchBoxModel'

export const SEARCH_DEBOUNCE_MS = 300

export interface DebouncedSearch {
  /** What the box shows (controlled input value). */
  text: string
  /** The input's onChange. */
  onChange: (text: string) => void
  /** Empties the box and commits the empty search at once (the box's own clear button). */
  clear: () => void
  /** Empties the box without committing, for an action that clears the address itself ("Clear all"). */
  reset: () => void
  /** Commits a waiting text now (Enter). */
  flush: () => void
  /** A commit is waiting for the quiet time to pass. */
  pending: boolean
}

/**
 * The one way an Inventory search box talks to the address bar.
 *
 * - `value` is the search the address carries NOW (the URL's `q`);
 * - `onCommit(q)` receives the trimmed text once the user has paused for `delay` ms. Apply it to the LIVE
 *   address as a patch of `q` alone (`useInventoryUrlState().patchFilters({ q })` or `setFilters(live => ...)`),
 *   never with a filter object captured at render: a facet or switch changed while the text waited must survive.
 *
 * The box is not overwritten by the echo of its own commit, follows the address when Back, a chip or
 * "Clear all" changes it, and drops a waiting commit when that happens (see ./searchBoxModel.ts, which is
 * tested with a fake clock). Every Inventory search box (Assets, Changes, Software, a product's Installed
 * tab) uses this hook.
 */
export function useDebouncedSearch({ value, onCommit, delay = SEARCH_DEBOUNCE_MS }: { value: string; onCommit: (q: string) => void; delay?: number }): DebouncedSearch {
  const ref = useRef<SearchController | null>(null)
  if (ref.current === null) ref.current = createSearchController({ value, onCommit, delay })
  const ctl = ref.current
  // The newest callback and delay, without re-creating the controller (a timer started earlier must call the live one).
  ctl.setOnCommit(onCommit)
  ctl.setDelay(delay)

  const text = useSyncExternalStore(ctl.subscribe, ctl.getText, ctl.getText)
  const pending = useSyncExternalStore(ctl.subscribe, ctl.isPending, ctl.isPending)
  useEffect(() => { ctl.syncUrl(value) }, [ctl, value])
  useEffect(() => () => ctl.dispose(), [ctl])

  return {
    text,
    pending,
    onChange: ctl.type,
    clear: ctl.clear,
    reset: ctl.reset,
    flush: ctl.flush,
  }
}
