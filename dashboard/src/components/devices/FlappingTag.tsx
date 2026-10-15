import { Waves } from 'lucide-react'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { flapBadgeText, flapTooltip, type DeviceFlapFields } from './deviceStatus'

/**
 * The Flapping marker beside a device's confirmed status (never instead of it): dashed amber, with the kind, since
 * when, and that status notifications are held in its tooltip. `title` replaces the tooltip when the caller knows
 * more (the device page passes the episode's counters and earliest end).
 */
export function FlappingTag({ device, title, withKind = false, className }: {
  device: DeviceFlapFields
  title?: string
  withKind?: boolean
  className?: string
}) {
  const text = title ?? flapTooltip({ kind: device.flap_kind, since: device.flapping_since }, serverNow())
  return (
    <span
      title={text}
      className={cn('inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-dashed border-warning/50 bg-warning/10 px-1.5 text-[10px] font-medium text-warning', className)}
    >
      <Waves className="h-2.5 w-2.5" aria-hidden />
      {flapBadgeText(device.flap_kind, withKind)}
    </span>
  )
}
