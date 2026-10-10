import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

/** The shape of the settings form while it loads (a separate file: the form itself is loaded on demand). */
export function SettingsEditorSkeleton() {
  return (
    <div className="grid gap-4 lg:grid-cols-2" aria-busy="true" aria-label="Loading settings">
      {[0, 1, 2].map((i) => (
        <Card key={i} className={cn('space-y-3 p-4', i === 2 && 'lg:col-span-2')}>
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-9 w-28" />
          <Skeleton className="h-9 w-28" />
          <Skeleton className="h-3 w-64 max-w-full" />
        </Card>
      ))}
    </div>
  )
}
