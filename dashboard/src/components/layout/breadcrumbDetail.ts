import { useEffect, useSyncExternalStore } from 'react'
import { useLocation } from 'react-router-dom'

// The name of the record a detail page shows (a product, a device...), for the last crumb of the header's
// breadcrumb. The trail itself comes from the navigation tree, which knows the section a route belongs to but
// not the record it shows, so the page tells the layout: `useBreadcrumbDetail(name)` in the page,
// `useBreadcrumbDetailFor(pathname)` in the breadcrumb. Keyed by the address so a name never outlives its page
// (while the next page loads, the crumb falls back to "Detail"). Module state, no provider: one header per app.

interface Entry { path: string; label: string }

let entry: Entry | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((l) => l())

/** Sets (or, with `null`, clears) the name for an address. Clearing only removes the entry that is still this address's. */
export function setBreadcrumbDetail(path: string, label: string | null): void {
  if (label === null || label.trim() === '') {
    if (entry?.path === path) { entry = null; emit() }
    return
  }
  const next = label.trim()
  if (entry?.path === path && entry.label === next) return
  entry = { path, label: next }
  emit()
}

/** The name registered for exactly this address, else null. */
export function breadcrumbDetailFor(path: string): string | null {
  return entry && entry.path === path ? entry.label : null
}

const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }

/** For the breadcrumb: the name of the record shown at this address, if its page registered one. */
export function useBreadcrumbDetailFor(pathname: string): string | null {
  return useSyncExternalStore(subscribe, () => breadcrumbDetailFor(pathname), () => null)
}

/** For a detail page: name the record shown here (null while it loads or when it does not exist). */
export function useBreadcrumbDetail(label: string | null | undefined): void {
  const { pathname } = useLocation()
  useEffect(() => {
    setBreadcrumbDetail(pathname, label ?? null)
    return () => setBreadcrumbDetail(pathname, null)
  }, [pathname, label])
}
