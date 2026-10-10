import { useCallback, useState } from 'react'
import { useAuth } from '@/stores/auth'
import { DEFAULT_PREFS, parsePrefs, prefsKey, serializePrefs, type AssetViewPrefs } from './assetModel'

function read(key: string): AssetViewPrefs {
  try {
    return parsePrefs(localStorage.getItem(key))
  } catch {
    // Private window or blocked storage: the defaults, and the page works without remembering anything.
    return { ...DEFAULT_PREFS, columns: [...DEFAULT_PREFS.columns] }
  }
}

/**
 * The per-user layout of the asset table (columns, density, facet pane), kept in the browser. It is a
 * convenience, never state that matters: every read and write is guarded, and nothing breaks when the
 * storage is unavailable.
 */
export function useAssetPrefs(): [AssetViewPrefs, (update: (current: AssetViewPrefs) => AssetViewPrefs) => void] {
  const userId = useAuth((s) => s.user?.id)
  const key = prefsKey(userId)
  const [state, setState] = useState(() => ({ key, prefs: read(key) }))
  // The account can change under a mounted page (sign-out, sign-in): follow the key.
  const prefs = state.key === key ? state.prefs : read(key)
  const update = useCallback(
    (fn: (current: AssetViewPrefs) => AssetViewPrefs) => {
      setState((cur) => {
        const base = cur.key === key ? cur.prefs : read(key)
        const next = fn(base)
        try { localStorage.setItem(key, serializePrefs(next)) } catch { /* the choice lasts for this visit only */ }
        return { key, prefs: next }
      })
    },
    [key],
  )
  return [prefs, update]
}
