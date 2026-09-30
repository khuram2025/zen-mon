import { Bell } from 'lucide-react'
import { relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import type { Alert } from '../types'

// ─────────────────────────────────────────────────────────────────
// Alerts panel
// ─────────────────────────────────────────────────────────────────

function AlertsPanel({ alerts }: { alerts: Alert[] }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <Bell className="h-4 w-4 text-warning" />
          <div>
            <CardTitle>Alerts / Incidents</CardTitle>
            <p className="text-xs text-muted">Derived from flow telemetry in this window</p>
          </div>
        </div>
        <Badge variant={alerts.some((a) => a.severity === 'critical') ? 'danger' : alerts.length ? 'warning' : 'success'}>{alerts.length}</Badge>
      </CardHeader>
      <CardContent className="space-y-2">
        {alerts.length === 0 && (
          <div className="rounded-md border border-dashed border-border bg-surface2/30 p-4 text-center text-xs text-muted">
            No active incidents.
          </div>
        )}
        {alerts.map((a) => {
          const palette =
            a.severity === 'critical'
              ? { dot: 'bg-rose-500 shadow-[0_0_10px_rgba(244,63,94,0.7)]', icon: 'bg-rose-500/15 text-rose-300', side: 'border-l-rose-500/70' }
              : a.severity === 'warning'
              ? { dot: 'bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.7)]', icon: 'bg-amber-400/15 text-amber-300', side: 'border-l-amber-400/70' }
              : a.severity === 'success'
              ? { dot: 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.6)]', icon: 'bg-emerald-400/15 text-emerald-300', side: 'border-l-emerald-400/70' }
              : { dot: 'bg-cyan-400 shadow-[0_0_10px_rgba(34,211,238,0.6)]', icon: 'bg-cyan-400/15 text-cyan-300', side: 'border-l-cyan-400/70' }
          const Icon = a.icon
          return (
            <div key={a.id} className={`flex gap-2.5 rounded-md border border-border border-l-2 bg-surface2/40 p-2.5 ${palette.side}`}>
              <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${palette.icon}`}>
                <Icon className="h-3.5 w-3.5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className={`h-1.5 w-1.5 rounded-full ${palette.dot}`} />
                  <span className="text-xs font-medium text-text">{a.title}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-muted">{a.body}</div>
                {a.ts && <div className="mt-0.5 text-[10px] text-muted">{relativeTime(a.ts)}</div>}
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

export { AlertsPanel }
