// What to do when a lazy page's file cannot be downloaded (review finding NF-01).
//
// The dashboard is built with hashed file names. After an update the old files are gone, so a tab that was opened
// before the update asks for chunks that no longer exist (the server answers with index.html, or a 404), and a
// flaky link fails the same way. React.lazy caches the rejected promise, so "Try again" re-renders into the same
// error and so does leaving the page and coming back: only loading the page again recovers.
//
// This file is the decision logic and nothing else (no React, no window): is this a chunk-load error, and may the
// page reload itself once without looping. components/ErrorBoundary.tsx wires it to the browser and
// scripts/test-inventory-ui.mjs tests it.

/** The messages browsers give a failed `import()` / chunk request, by engine. */
const CHUNK_MESSAGES: readonly RegExp[] = [
  /failed to fetch dynamically imported module/i, // Chrome, Edge (and the Vite dev server)
  /error loading dynamically imported module/i, // Firefox
  /importing a module script failed/i, // Safari
  /failed to load module script/i, // the server answered a module request with HTML (the SPA fallback)
  /is not a valid javascript mime type/i,
  /loading chunk [\w.-]+ failed/i, // webpack-style loaders
  /loading css chunk [\w.-]+ failed/i,
  /unable to preload css/i, // Vite's own CSS preload
]

/** True when `error` (or an error it was caused by) is a failed dynamic import of a page's file. */
export function isChunkLoadError(error: unknown, depth = 0): boolean {
  if (error === null || error === undefined || depth > 3) return false
  if (typeof error === 'string') return CHUNK_MESSAGES.some((re) => re.test(error))
  if (typeof error !== 'object') return false
  const e = error as { name?: unknown; message?: unknown; cause?: unknown }
  if (e.name === 'ChunkLoadError') return true
  if (typeof e.message === 'string' && CHUNK_MESSAGES.some((re) => re.test(e.message as string))) return true
  return e.cause !== undefined && isChunkLoadError(e.cause, depth + 1)
}

/** sessionStorage key of the last automatic reload (a millisecond timestamp). */
export const CHUNK_RELOAD_KEY = 'zp-chunk-reload'
/**
 * One automatic reload per this long. A reload that does not fix the problem (the files really are missing) must
 * not become a loop: inside the window the page shows the recovery notice and waits for the person to press the button.
 */
export const CHUNK_RELOAD_WINDOW_MS = 5 * 60_000

/** The part of `Storage` this needs. */
export interface ReloadGuardStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** May the page reload itself now? `record` is the stored timestamp of the last automatic reload, if any. */
export function mayAutoReload(record: string | null | undefined, now: number, windowMs = CHUNK_RELOAD_WINDOW_MS): boolean {
  if (record === null || record === undefined || record === '') return true
  const last = Number(record)
  if (!Number.isFinite(last)) return true
  // |now - last|: a clock that moved backwards does not open the gate early either.
  return Math.abs(now - last) >= windowMs
}

export type ChunkRecovery = 'not-a-chunk-error' | 'reloading' | 'manual'

/**
 * The one decision: for `error`, reload now (once per window, recorded in `storage` BEFORE the reload so a failure
 * after it cannot loop), or leave the recovery notice and its button to the person. Storage that cannot be read or
 * written means the guard cannot be kept, so the answer is `manual`, never an unguarded reload.
 */
export function recoverFromChunkError(
  error: unknown,
  io: { storage: ReloadGuardStorage | null; now: number; reload: () => void },
): ChunkRecovery {
  if (!isChunkLoadError(error)) return 'not-a-chunk-error'
  if (!io.storage) return 'manual'
  try {
    if (!mayAutoReload(io.storage.getItem(CHUNK_RELOAD_KEY), io.now)) return 'manual'
    io.storage.setItem(CHUNK_RELOAD_KEY, String(io.now))
  } catch {
    return 'manual'
  }
  io.reload()
  return 'reloading'
}

/** The browser's session storage, or null where it is unavailable (private window, blocked site data). */
export function browserSessionStorage(): ReloadGuardStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}
