import { AlertTriangle, CheckCircle2, CircleDashed, Wrench, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

/* The confirmed status and the health rollup are two separate indicators.
 * The status comes from the confirmation machine (Up / Degraded / Down) and is
 * never recoloured by template readings; the rollup counts what needs
 * attention right now. Every colour is paired with an icon so the state does
 * not depend on colour alone. */

export type StatusTone = 'ok' | 'warning' | 'critical' | 'maintenance' | 'unknown'

const STATUS: Record<string, { label: string; tone: StatusTone }> = {
  up: { label: 'Up', tone: 'ok' },
  degraded: { label: 'Degraded', tone: 'warning' },
  down: { label: 'Down', tone: 'critical' },
  maintenance: { label: 'Maintenance', tone: 'maintenance' },
}

export function deviceStatusView(status: string | null | undefined): { label: string; tone: StatusTone } {
  return STATUS[(status || '').toLowerCase()] || { label: status ? status[0].toUpperCase() + status.slice(1) : 'Unknown', tone: 'unknown' }
}

export const TONE_CLASS: Record<StatusTone, string> = {
  ok: 'border-success/30 bg-success/10 text-success',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  critical: 'border-danger/30 bg-danger/10 text-danger',
  maintenance: 'border-primary/30 bg-primary/10 text-primary',
  unknown: 'border-border bg-surface2 text-muted',
}

const TONE_ICON: Record<StatusTone, React.ComponentType<{ className?: string }>> = {
  ok: CheckCircle2,
  warning: AlertTriangle,
  critical: XCircle,
  maintenance: Wrench,
  unknown: CircleDashed,
}

export function DeviceStatusBadge({ status, since, className }: { status: string; since?: string | null; className?: string }) {
  const view = deviceStatusView(status)
  const Icon = TONE_ICON[view.tone]
  return (
    <span className={cn('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm font-medium', TONE_CLASS[view.tone], className)}>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      <span>{view.label}</span>
      {since && <span className="font-normal opacity-80">· {since}</span>}
    </span>
  )
}

/** "▲ 3 issues" / "No open issues". A button when it can jump to the issues list. */
export function HealthRollupChip({
  state, count, onClick, className,
}: { state: 'ok' | 'warning' | 'critical'; count: number; onClick?: () => void; className?: string }) {
  const tone: StatusTone = state === 'critical' ? 'critical' : state === 'warning' ? 'warning' : 'ok'
  const Icon = count ? AlertTriangle : CheckCircle2
  const body = (
    <>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {count ? `${count} issue${count === 1 ? '' : 's'}` : 'No open issues'}
    </>
  )
  const cls = cn('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm font-medium', count ? TONE_CLASS[tone] : 'border-border bg-surface2 text-muted', className)
  if (!onClick || !count) return <span className={cls}>{body}</span>
  return <button type="button" onClick={onClick} className={cn(cls, 'hover:brightness-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary')}>{body}</button>
}
