/**
 * Per-user report preferences in browser storage: favourites, recently
 * viewed reports and the library view mode. Preferences only — never data
 * that must be shared or persisted reliably. Every access tolerates storage
 * being unavailable (private windows, blocked site data).
 */

/** A report reference: a built-in template key, or `custom:<uuid>`. */
export type ReportRef = string

export const refFor = (key: string, customId?: string | null): ReportRef =>
  key === 'custom' && customId ? `custom:${customId}` : key

export function parseRef(ref: ReportRef): { key: string; customId: string | null } {
  return ref.startsWith('custom:') ? { key: 'custom', customId: ref.slice(7) } : { key: ref, customId: null }
}

const k = (userId: string | undefined, name: string) => `zp.reports.${name}.${userId || 'anon'}`

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable: preferences are best-effort */
  }
  try {
    window.dispatchEvent(new CustomEvent('zp-report-prefs', { detail: key }))
  } catch {
    /* non-browser */
  }
}

export function getFavorites(userId?: string): ReportRef[] {
  const v = read<unknown>(k(userId, 'favorites'), [])
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, 200) : []
}

export function toggleFavorite(userId: string | undefined, ref: ReportRef): ReportRef[] {
  const cur = getFavorites(userId)
  const next = cur.includes(ref) ? cur.filter((r) => r !== ref) : [ref, ...cur]
  write(k(userId, 'favorites'), next)
  return next
}

export interface RecentEntry { ref: ReportRef; at: number }

export function getRecent(userId?: string): RecentEntry[] {
  const v = read<unknown>(k(userId, 'recent'), [])
  return Array.isArray(v)
    ? v.filter((x): x is RecentEntry => !!x && typeof (x as RecentEntry).ref === 'string' && typeof (x as RecentEntry).at === 'number').slice(0, 20)
    : []
}

/** Called by the viewer when a report is opened. */
export function recordRecent(userId: string | undefined, ref: ReportRef, at = Date.now()): void {
  const next = [{ ref, at }, ...getRecent(userId).filter((e) => e.ref !== ref)].slice(0, 20)
  write(k(userId, 'recent'), next)
}

export type LibraryView = 'grid' | 'table'

export function getLibraryView(userId?: string): LibraryView {
  return read<string>(k(userId, 'view'), 'grid') === 'table' ? 'table' : 'grid'
}

export function setLibraryView(userId: string | undefined, v: LibraryView): void {
  write(k(userId, 'view'), v)
}
