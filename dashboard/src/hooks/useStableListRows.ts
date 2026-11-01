import { useLayoutEffect, useRef } from 'react'

/** Keep the committed row sequence during selection, while accepting new cell
 * values. A deliberate filter, page or sort change starts a new sequence. */
export function useStableListRows<T>(rows: T[], idOf: (row: T) => string, hold: boolean, scope: string): T[] {
  const previous = useRef<{ scope: string; ids: string[] }>()
  let ordered = rows
  if (hold && previous.current?.scope === scope) {
    const positions = new Map(previous.current.ids.map((id, i) => [id, i]))
    ordered = [...rows].sort((a, b) => (positions.get(idOf(a)) ?? Number.MAX_SAFE_INTEGER) - (positions.get(idOf(b)) ?? Number.MAX_SAFE_INTEGER))
  }
  useLayoutEffect(() => { previous.current = { scope, ids: ordered.map(idOf) } })
  return ordered
}
