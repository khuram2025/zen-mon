/**
 * Unsaved-changes guard for the template designer.
 *
 * The app uses <BrowserRouter> (not a data router), so react-router's
 * useBlocker is unavailable. Instead:
 *  - beforeunload asks before a reload, tab close or external navigation;
 *  - in-app links (sidebar, breadcrumbs, the designer's own links) are
 *    intercepted in the capture phase and held until the user confirms.
 * Programmatic navigation (navigate()) and the browser Back button are not
 * intercepted.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export function useUnsavedGuard(when: boolean) {
  const navigate = useNavigate()
  const [pending, setPending] = useState<string | null>(null)
  const bypass = useRef(false)
  const active = when && !bypass.current

  useEffect(() => {
    if (!active) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (bypass.current) return
      e.preventDefault()
      e.returnValue = ''
    }
    const onClick = (e: MouseEvent) => {
      if (bypass.current || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
      if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return
      let url: URL
      try {
        url = new URL(a.href, window.location.href)
      } catch {
        return
      }
      if (url.origin !== window.location.origin) return
      if (url.pathname === window.location.pathname && url.search === window.location.search) return
      e.preventDefault()
      e.stopPropagation()
      setPending(`${url.pathname}${url.search}${url.hash}`)
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    document.addEventListener('click', onClick, true)
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload)
      document.removeEventListener('click', onClick, true)
    }
  }, [active])

  /** Stop guarding (after a save or a confirmed discard), then optionally navigate. */
  const release = useCallback((to?: string) => {
    bypass.current = true
    setPending(null)
    if (to) navigate(to)
  }, [navigate])

  /** Navigate from code, asking first when there are unsaved changes. */
  const guardedNavigate = useCallback((to: string) => {
    if (when && !bypass.current) setPending(to)
    else navigate(to)
  }, [navigate, when])

  return {
    /** Where the user tried to go; non-null while the confirmation is open. */
    pending,
    confirm: () => release(pending ?? undefined),
    cancel: () => setPending(null),
    release,
    guardedNavigate,
  }
}
