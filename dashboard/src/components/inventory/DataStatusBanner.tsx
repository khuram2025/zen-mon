import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import type { DataStatus } from '@/pages/inventory/types'
import { fmtInt } from './model'

/** The sentence of the banner. One place, so every page and every test says the same thing. */
export const NORMALISING_TEXT = 'Normalising existing software lists; figures are incomplete'
/** Announced once when the backlog clears. */
export const NORMALISED_TEXT = 'Software lists are processed; figures are complete'

/** The second sentence of the banner: how much was left at the last check, and what happens when it is done. */
export function backlogText(status: Pick<DataStatus, 'pending_software_rows' | 'pending_identity_rows'>): string | null {
  const parts: string[] = []
  if (status.pending_software_rows > 0) parts.push(`${fmtInt(status.pending_software_rows)} software ${status.pending_software_rows === 1 ? 'row' : 'rows'}`)
  if (status.pending_identity_rows > 0) parts.push(`${fmtInt(status.pending_identity_rows)} ${status.pending_identity_rows === 1 ? 'asset' : 'assets'}`)
  if (parts.length === 0) return null
  // The page checks every 15 s and refetches its data when the backlog clears (useRefreshWhenNormalised): say exactly that.
  return `${parts.join(' and ')} still to process at the last check. The figures refresh when processing finishes.`
}

/**
 * Shown on every Inventory page while the normaliser is still working through rows that arrived before it (or before
 * a newer rule pack): software titles, versions and endpoint-protection coverage are partial until it is done, and a
 * smaller number must not be read as a finding. `data_status` is scope-aware (a scoped user's counts cover their own
 * assets). Nothing renders when the backlog is empty or the summary has not arrived.
 *
 * Only the STATE is a live region: the pending counts change on every 15 s poll and would be announced again and
 * again, so they sit in the visible banner outside it (review findings NF-10, NF-04). The live region exists
 * before the banner does, so a screen reader hears the sentence when the backlog appears and once more when it clears.
 */
export function DataStatusBanner({ status }: { status: DataStatus | null | undefined }) {
  const normalising = !!status?.normalising
  const was = useRef(false)
  const [finished, setFinished] = useState(false)
  useEffect(() => {
    if (was.current && !normalising) setFinished(true)
    if (normalising) setFinished(false)
    was.current = normalising
  }, [normalising])

  const backlog = status?.normalising ? backlogText(status) : null
  return (
    <>
      <div role="status" className="sr-only" data-data-status-live="">{normalising ? NORMALISING_TEXT : finished ? NORMALISED_TEXT : ''}</div>
      {normalising && (
        <div data-data-status="normalising" className="flex items-start gap-2 rounded-md border border-info/30 bg-info/10 px-3 py-2 text-xs text-info-fg">
          <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden />
          <p>
            <strong className="font-semibold">{NORMALISING_TEXT}.</strong>
            {backlog && <> {backlog}</>}
          </p>
        </div>
      )}
    </>
  )
}
