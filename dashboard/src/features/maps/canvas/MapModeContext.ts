import { createContext, useContext } from 'react'

/** Shares editor-wide display state with custom node/edge components without
 *  rebuilding React Flow node data (which would drop optimistic edits).
 *  `textBoost` enlarges on-canvas text on wall displays (1 = desk). `pinnedId`
 *  is the node whose inspector is open, so its hover card stays hidden. */
export const MapModeContext = createContext<{ connectMode: boolean; textBoost?: number; pinnedId?: string | null; wall?: boolean }>({ connectMode: false })
export const useMapMode = () => useContext(MapModeContext)
