import type { ReactNode } from 'react'
import { Inbox, SearchX, ShieldOff } from 'lucide-react'
import { cn } from '@/lib/utils'

export type InventoryEmptyKind = 'no-data' | 'no-match' | 'no-scope'

const COPY: Record<InventoryEmptyKind, { icon: typeof Inbox; title: string }> = {
  // Nothing has been collected yet: say what will appear and how to make it appear.
  'no-data': { icon: Inbox, title: 'Nothing here yet' },
  // Data exists, the filters excluded all of it: offer the way back.
  'no-match': { icon: SearchX, title: 'Nothing matches these filters' },
  // Data exists, but none of it is inside the caller's tag scope.
  'no-scope': { icon: ShieldOff, title: 'Nothing in your scope' },
}

/**
 * The three honest ways a list or panel can be empty. They are different facts and read
 * differently: "no data yet" is not "your filters matched nothing" is not "your scope hides it".
 * A failed request is not an empty state at all: use QueryFailure / QueryBoundary for that.
 */
export function InventoryEmpty({
  kind = 'no-data',
  title,
  children,
  action,
  compact,
  className,
}: {
  kind?: InventoryEmptyKind
  title?: string
  children?: ReactNode
  action?: ReactNode
  compact?: boolean
  className?: string
}) {
  const { icon: Icon, title: defaultTitle } = COPY[kind]
  return (
    <div
      data-empty={kind}
      className={cn('flex flex-col items-center gap-1.5 text-center', compact ? 'py-4' : 'py-10', className)}
    >
      <Icon className={cn('text-muted/60', compact ? 'h-5 w-5' : 'h-7 w-7')} aria-hidden />
      <div className="text-sm font-medium text-text2">{title ?? defaultTitle}</div>
      {children && <div className="max-w-md text-xs text-muted">{children}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}
