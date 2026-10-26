import { cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { ageWords, sourceLabel } from './model'

/**
 * Where an identity value came from: Agent, SNMP, Template, ENTITY-MIB, WinRM, SSH. Shown next to
 * every identity value in the asset flyout ("FortiOS 7.2.5, from FortiGate template, 4 min ago").
 */
export function SourceBadge({
  source,
  at,
  showAge = false,
  now,
  className,
}: {
  source: string | null | undefined
  /** When the value was last written by that source. */
  at?: string | null
  showAge?: boolean
  now?: number
  className?: string
}) {
  const label = sourceLabel(source)
  const age = at ? ageWords(at, now ?? serverNow()) : null
  return (
    <span
      title={age ? `${label}, ${age}` : label}
      data-source={source ?? ''}
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded border border-border bg-surface2 px-1.5 py-px text-xs font-medium leading-4 text-text2',
        // No source is still a value; italic keeps it apart without dropping below AA (muted on surface2 is 4.3:1).
        !source && 'italic',
        className,
      )}
    >
      {label}
      {/* text-text2, not text-muted: muted on the badge's surface2 fill is 4.3:1 in the light theme (NF-1). */}
      {showAge && age && <span className="font-normal text-text2">· {age}</span>}
    </span>
  )
}
