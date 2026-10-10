// The state machine behind every Inventory search box (Assets, Changes, Software, a product's Installed tab),
// with no React and no timers of its own: scripts/test-inventory-ui.mjs drives it with a fake clock.
// `useDebouncedSearch` is the thin React wrapper; a search box must not be written any other way.
//
// What it guarantees (review findings FE-02 and FE-07):
//  1. The box is never overwritten by the echo of its OWN last commit. A search box that mirrors the address
//     bar with an unconditional `useEffect(() => setText(q), [q])` next to a debounce drops a character typed
//     between the commit and the router re-render (the box is reset to the older committed text).
//  2. The box still follows the address when something ELSE changes it: Back, Forward, a removed chip or
//     "Clear all". Such a change also cancels a commit that was waiting.
//  3. It commits the search text and nothing else (`onCommit(q)`); the caller applies it to the LIVE address
//     (see urlState.createUrlWriter), so a facet clicked while the commit was waiting is never undone.
//  4. Typing text that equals what the address already says commits nothing; a commit is never repeated.
//  5. "What the address already says" means what it WILL say once the box's own commits have landed (the last
//     commit still on its way to the router), not only what it says now. Without that, backspacing to the old
//     text inside the router's echo latency is taken for "nothing to commit": the echo then sets the address to
//     the newer text while the box shows the older one and nothing is pending (NF-FE-2, NF-03).

/** Collapses white space and trims: how the filters read a search, so `a  b ` echoes back as `a b`. */
export const normaliseSearch = (s: string): string => s.replace(/\s+/g, ' ').trim()

export interface Scheduler {
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(handle: unknown): void
}

const BROWSER: Scheduler = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
}

export interface SearchControllerOptions {
  /** The search the address bar carries now. */
  value: string
  /** Quiet time after the last keystroke before the text is committed. */
  delay?: number
  /** Receives the (trimmed) text to apply. Read at call time, so it may change between renders. */
  onCommit: (q: string) => void
  /** Called after every state change (React re-reads `getText`). */
  onChange?: () => void
  /** Injectable timers for tests. */
  scheduler?: Scheduler
  /** How the page reads a search back from the address (default {@link normaliseSearch}). */
  normalise?: (s: string) => string
}

export interface SearchController {
  /** What the box shows. */
  getText(): string
  /** True while a commit is waiting for the quiet time to pass. */
  isPending(): boolean
  /** The user typed. */
  type(text: string): void
  /** The address bar's search changed (call it from an effect on the `value` prop; calling it with the same value is harmless). */
  syncUrl(value: string): void
  /** The box's clear button: empties the box and commits the empty search at once. */
  clear(): void
  /** Empties the box WITHOUT committing, for an action that clears the address itself ("Clear all"). Cancels a waiting commit. */
  reset(): void
  /** Commits a waiting text now (Enter, blur). */
  flush(): void
  /** Drops the timer (unmount). The controller stays usable: React StrictMode re-runs effects. */
  dispose(): void
  setOnCommit(fn: (q: string) => void): void
  setDelay(ms: number): void
  subscribe(listener: () => void): () => void
}

/** Echoes remembered at most: a commit whose echo never comes back (a value the page refused) must not pile up. */
const MAX_IN_FLIGHT = 8
/**
 * How long a commit waits for its echo before it is forgotten. A value the page refused, or two writes the router
 * folded into one render, never echo; remembering them would make the box believe the address says something it
 * does not (and take a later Back to that value for its own echo).
 */
export const ECHO_EXPIRY_MS = 3000

export function createSearchController(options: SearchControllerOptions): SearchController {
  const scheduler = options.scheduler ?? BROWSER
  const norm = options.normalise ?? normaliseSearch
  let onCommit = options.onCommit
  let delay = options.delay ?? 300

  let text = options.value
  let url = options.value
  let pending: string | null = null
  let timer: unknown = null
  // Searches this box committed whose echo (the address changing to that value) has not been seen yet, oldest first.
  let inFlight: { value: string; timer: unknown }[] = []
  const listeners = new Set<() => void>()

  const emit = () => {
    options.onChange?.()
    for (const l of [...listeners]) l()
  }
  const disarm = () => {
    if (timer !== null) scheduler.clearTimeout(timer)
    timer = null
  }
  const arm = () => {
    disarm()
    timer = scheduler.setTimeout(() => {
      timer = null
      commitPending()
    }, delay)
  }
  /** The search the address will carry once every commit of this box has landed: the last one on its way, else what it says now. */
  const expected = (): string => (inFlight.length > 0 ? inFlight[inFlight.length - 1].value : norm(url))
  const forget = (entries: { timer: unknown }[]) => {
    for (const e of entries) scheduler.clearTimeout(e.timer)
  }
  function commit(q: string) {
    // Remember the value the address will carry, so its arrival is recognised as ours.
    const entry = { value: norm(q), timer: null as unknown }
    entry.timer = scheduler.setTimeout(() => { inFlight = inFlight.filter((e) => e !== entry) }, ECHO_EXPIRY_MS)
    const kept = [...inFlight, entry]
    forget(kept.slice(0, Math.max(0, kept.length - MAX_IN_FLIGHT)))
    inFlight = kept.slice(-MAX_IN_FLIGHT)
    onCommit(q)
  }
  function commitPending() {
    const q = pending
    pending = null
    if (q === null) return
    // The address may have caught up (or been changed from outside) while the text waited.
    if (norm(q) === expected()) {
      emit()
      return
    }
    commit(q)
    emit()
  }

  return {
    getText: () => text,
    isPending: () => pending !== null,
    type(next) {
      text = next
      const trimmed = next.trim()
      if (norm(trimmed) === expected()) {
        // Back to what the address says (or is about to say): nothing to commit.
        pending = null
        disarm()
      } else {
        pending = trimmed
        arm()
      }
      emit()
    },
    syncUrl(value) {
      if (value === url) return
      url = value
      const mine = inFlight.findIndex((e) => e.value === norm(value))
      if (mine >= 0) {
        // The echo of something this box committed: keep what the user has typed since.
        forget(inFlight.slice(0, mine + 1))
        inFlight = inFlight.slice(mine + 1)
        if (pending !== null && norm(pending) === norm(value) && expected() === norm(value)) {
          pending = null
          disarm()
        }
        emit()
        return
      }
      // Changed from outside (Back, a chip, Clear all): follow it, and forget a commit that was waiting.
      forget(inFlight)
      inFlight = []
      pending = null
      disarm()
      text = value
      emit()
    },
    clear() {
      text = ''
      pending = null
      disarm()
      if (expected() !== '') commit('')
      emit()
    },
    reset() {
      text = ''
      pending = null
      forget(inFlight)
      inFlight = []
      disarm()
      emit()
    },
    flush() {
      disarm()
      commitPending()
    },
    dispose() {
      disarm()
      forget(inFlight)
      inFlight = []
    },
    setOnCommit(fn) {
      onCommit = fn
    },
    setDelay(ms) {
      delay = ms
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
