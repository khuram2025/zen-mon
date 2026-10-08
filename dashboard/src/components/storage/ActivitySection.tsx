import { Activity, Loader2 } from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { EVENT_LABELS, eventSummary, groupEvents } from './storageMath'
import { useStorageEvents } from './useStorage'

function tone(type: string) {
  if (type.includes('failed')) return 'text-danger'
  if (type === 'auto_purge' || type === 'auto_purge_skipped' || type.startsWith('disk_evacuation')) return 'text-warning'
  if (type === 'disk_added' || type === 'volume_extended' || type === 'space_claimed') return 'text-success'
  return 'text-text'
}

export function ActivitySection() {
  const { data: events, isLoading } = useStorageEvents(100)
  const grouped = groupEvents(events || [])

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-primary" /> Storage activity
        </CardTitle>
        <p className="text-xs text-muted">
          Disk changes, purges, retention changes, cleanups and backups. Repeated identical entries are collapsed.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
        ) : grouped.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border/60 p-6 text-center text-xs text-muted">
            No storage activity yet.
          </div>
        ) : (
          <ol className="space-y-1">
            {grouped.map(({ event: e, count, oldest }) => {
              const summary = eventSummary(e)
              return (
                <li key={e.id} className="flex items-start justify-between gap-4 rounded-md px-2 py-1.5 text-xs hover:bg-surface2/40">
                  <div className="min-w-0">
                    <span className={cn('font-medium', tone(e.event_type))}>
                      {EVENT_LABELS[e.event_type] || e.event_type}
                    </span>
                    {count > 1 && (
                      <span className="ml-1.5 rounded bg-surface2 px-1.5 py-0.5 text-[10px] tabular-nums text-text2">×{count}</span>
                    )}
                    <span className="ml-2 text-muted">
                      {e.freed_bytes > 0 && <>freed {e.freed_human} · </>}
                      {summary && <>{summary} · </>}
                      by {e.actor}
                    </span>
                  </div>
                  <span className="shrink-0 tabular-nums text-muted" title={new Date(e.created_at).toLocaleString()}>
                    {count > 1 ? `${relativeTime(oldest)} – ${relativeTime(e.created_at)}` : relativeTime(e.created_at)}
                  </span>
                </li>
              )
            })}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}
