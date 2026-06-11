import { createContext, useContext } from 'react'

/** Shares the editor's connect-mode with custom node components without
 *  rebuilding React Flow node data (which would drop optimistic edits). */
export const MapModeContext = createContext<{ connectMode: boolean }>({ connectMode: false })
export const useMapMode = () => useContext(MapModeContext)
