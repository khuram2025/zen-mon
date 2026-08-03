/**
 * Small shared pieces for the alert surfaces (list, detail, device pages):
 * channel icons, the snooze menu, and message cleanup for legacy rows.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlarmClockOff, BellOff, Hash, Loader2, Mail, MessageSquare, Send, Webhook,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'

export type AlertChannel = {
  id: string
  name: string
  type: 'email' | 'sms' | 'webhook' | 'slack' | 'telegram' | string
  enabled: boolean
}

export const CHANNEL_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  email: Mail,
  sms: MessageSquare,
  webhook: Webhook,
  slack: Hash,
  telegram: Send,
}

/**
 * Icons for the channels a rule notifies. Disabled channels render dimmed —
 * an alert whose only channel is switched off is effectively silent, and the
 * operator should see that at a glance.
 */
export function ChannelIcons({ channels, className }: { channels?: AlertChannel[]; className?: string }) {
  if (!channels || channels.length === 0) {
    return (
      <span className={cn('inline-flex items-center gap-1 text-muted/60', className)} title="No notification channels attached">
        <BellOff className="h-3.5 w-3.5" />
      </span>
    )
  }
  return (
    <span className={cn('inline-flex items-center gap-1', className)}>
      {channels.map((c) => {
        const Icon = CHANNEL_ICONS[c.type] || Webhook
        return (
          <span
            key={c.id}
            title={`${c.name} (${c.type})${c.enabled ? '' : ' — disabled'}`}
            className={cn(
              'inline-flex h-5 w-5 items-center justify-center rounded bg-info/10 text-info',
              !c.enabled && 'bg-surface2 text-muted/50',
            )}
          >
            <Icon className="h-3 w-3" />
          </span>
        )
      })}
    </span>
  )
}

/** Legacy alert rows stored the rendered SMS template as the message. Strip
 *  the transport prefix so lists show the actual event. */
export function cleanAlertMessage(message: string | null | undefined): string {
  if (!message) return 'Alert'
  return message.replace(/^\[ZenPlus\s+[A-Z]+\]\s*/, '')
}

const SNOOZE_OPTIONS = [
  { label: '1 hour', minutes: 60 },
  { label: '6 hours', minutes: 360 },
  { label: '24 hours', minutes: 1440 },
  { label: '7 days', minutes: 10080 },
  { label: 'Forever (until cleared)', minutes: null as number | null },
]

/**
 * Snooze dropdown for an alert: suppresses re-raising of the same condition
 * (rule + device/interface, or server dedupe) until the chosen time. The
 * current alert is resolved as part of snoozing.
 */
export function SnoozeMenu({
  alertId, snoozed, size = 'sm', onDone,
}: {
  alertId: string
  snoozed?: boolean
  size?: 'sm' | 'default'
  onDone?: () => void
}) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  const snooze = useMutation({
    mutationFn: async (minutes: number | null) =>
      api.post(`/alerts/${alertId}/snooze`, { minutes }),
    onSuccess: (_d, minutes) => {
      toast.success(minutes ? `Snoozed — muted for ${SNOOZE_OPTIONS.find((o) => o.minutes === minutes)?.label || `${minutes}m`}` : 'Muted until cleared')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      setOpen(false)
      onDone?.()
    },
    onError: (e: any) => toast.error('Snooze failed', apiErrorMessage(e)),
  })

  const unsnooze = useMutation({
    mutationFn: async () => api.post(`/alerts/${alertId}/unsnooze`),
    onSuccess: () => {
      toast.success('Snooze cleared — condition will alert again')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      setOpen(false)
      onDone?.()
    },
    onError: (e: any) => toast.error('Unsnooze failed', apiErrorMessage(e)),
  })

  if (snoozed) {
    return (
      <Button size={size} variant="outline" onClick={() => unsnooze.mutate()} disabled={unsnooze.isPending} title="Condition is snoozed — click to clear">
        {unsnooze.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <AlarmClockOff className="h-3.5 w-3.5" />}
        Unsnooze
      </Button>
    )
  }

  return (
    <div className="relative inline-block" ref={ref}>
      <Button size={size} variant="outline" onClick={() => setOpen((v) => !v)} title="Mute this condition for a while">
        <BellOff className="h-3.5 w-3.5" /> Snooze
      </Button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-44 rounded-md border border-border bg-surface p-1 shadow-lg">
          {SNOOZE_OPTIONS.map((o) => (
            <button
              key={o.label}
              className="block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-surface2"
              disabled={snooze.isPending}
              onClick={() => snooze.mutate(o.minutes)}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
