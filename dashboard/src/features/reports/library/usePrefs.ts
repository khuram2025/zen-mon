/**
 * The signed-in user's report preferences (favourites, recently viewed and the
 * library layout) from browser storage, kept in step across components and
 * tabs through the `zp-report-prefs` and `storage` events.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAuth } from '@/stores/auth'
import {
  getFavorites, getLibraryView, getRecent, setLibraryView, toggleFavorite,
  type LibraryView, type ReportRef,
} from '../storage'

export function useReportPrefs() {
  const userId = useAuth((s) => s.user?.id)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const bump = () => setVersion((v) => v + 1)
    window.addEventListener('zp-report-prefs', bump)
    window.addEventListener('storage', bump)
    return () => {
      window.removeEventListener('zp-report-prefs', bump)
      window.removeEventListener('storage', bump)
    }
  }, [])

  // `version` re-reads storage after any write.
  const favorites = useMemo(() => getFavorites(userId), [userId, version])
  const recent = useMemo(() => getRecent(userId), [userId, version])
  const layout = useMemo(() => getLibraryView(userId), [userId, version])

  const toggle = useCallback((ref: ReportRef) => toggleFavorite(userId, ref), [userId])
  const setLayout = useCallback((v: LibraryView) => setLibraryView(userId, v), [userId])

  return { favorites, recent, layout, toggleFavorite: toggle, setLayout }
}
