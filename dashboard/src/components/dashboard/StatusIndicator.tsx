import { statusColors, statusLabels, cn } from '@/lib/utils'
import type { DeviceStatus } from '@/types'

interface StatusIndicatorProps {
  status: DeviceStatus
  showLabel?: boolean
  size?: 'sm' | 'md' | 'lg'
}

export function StatusIndicator({ status, showLabel = false, size = 'md' }: StatusIndicatorProps) {
  const sizeMap = {
    sm: 'w-2 h-2',
    md: 'w-2.5 h-2.5',
    lg: 'w-3 h-3',
  }

  return (
    <div className="flex items-center gap-2">
      <span
        className={cn('rounded-full', sizeMap[size])}
        style={{ backgroundColor: statusColors[status] }}
        aria-label={statusLabels[status]}
      />
      {showLabel && (
        <span
          className="text-sm font-medium"
          style={{ color: statusColors[status] }}
        >
          {statusLabels[status]}
        </span>
      )}
    </div>
  )
}
