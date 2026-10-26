import { cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import type { Freshness } from '@/pages/inventory/filters'
import { FRESHNESS_META, ageWords, classifyFreshness, freshnessTitle, type FreshnessThresholds } from './model'

/**
 * Fresh / Aging / Stale / Never / Not reported by design pill with the age in words ("Aging · 6h ago").
 * The label is always written out, so colour is never the only signal; "Not reported by design" has a dashed
 * outline and a hollow dot, and no age (such an asset has no inventory to be late with). The tooltip keeps *last inventory* (the data)
 * apart from *last seen* (reachability), which differ for an asset that answers but stopped
 * reporting software.
 */
export function FreshnessBadge({
  freshness,
  kind = 'server',
  inventoryAt,
  lastSeen,
  thresholds,
  showAge = true,
  now,
  className,
}: {
  /** The server's verdict. Omit it to derive one from `inventoryAt` and `kind`. */
  freshness?: Freshness
  kind?: 'server' | 'device'
  /** Time of the last inventory (data). */
  inventoryAt?: string | null
  /** Time of the last poll or heartbeat (reachability). */
  lastSeen?: string | null
  thresholds?: FreshnessThresholds
  showAge?: boolean
  /** Injectable clock for tests; defaults to the appliance-calibrated now. */
  now?: number
  className?: string
}) {
  const clock = now ?? serverNow()
  const state = freshness ?? classifyFreshness(kind, inventoryAt, clock, thresholds)
  const meta = FRESHNESS_META[state]
  const age = showAge && state !== 'never' && state !== 'not_applicable' && inventoryAt ? ageWords(inventoryAt, clock) : null
  return (
    <span
      title={freshnessTitle(state, inventoryAt, lastSeen, clock)}
      data-freshness={state}
      className={cn('inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium leading-4', meta.tone, className)}
    >
      <span aria-hidden className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
      {meta.label}
      {/* Full-strength badge colour: dimming it with opacity dropped the age below 4.5:1 in the light theme. */}
      {age && <span className="font-normal">· {age}</span>}
    </span>
  )
}
