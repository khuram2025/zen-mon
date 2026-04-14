import { cn } from '@/lib/utils'

export function StatusDot({
  status,
  pulse,
  className,
}: {
  status: 'up' | 'down' | 'warn' | 'info' | 'idle'
  pulse?: boolean
  className?: string
}) {
  return (
    <span
      className={cn(
        'status-dot',
        status === 'up' && 'status-dot-up',
        status === 'down' && 'status-dot-down',
        status === 'warn' && 'status-dot-warn',
        status === 'info' && 'status-dot-info',
        status === 'idle' && 'status-dot-idle',
        pulse && 'status-dot-live',
        className,
      )}
    />
  )
}

export function deviceStatusKind(status: string): 'up' | 'down' | 'warn' | 'info' | 'idle' {
  if (status === 'up') return 'up'
  if (status === 'down') return 'down'
  if (status === 'degraded') return 'warn'
  if (status === 'maintenance') return 'info'
  return 'idle'
}
