import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { Heatmap } from '../types'
import { EmptyState } from './EmptyState'

// ─────────────────────────────────────────────────────────────────
// Traffic Heatmap (24h × 7d)
// ─────────────────────────────────────────────────────────────────

function TrafficHeatmap({ data, activeHour, activeDow, onSelect }: { data?: Heatmap; activeHour?: number | null; activeDow?: number | null; onSelect?: (dow: number, hour: number) => void }) {
  const cells = data?.cells || []
  const max = Math.max(1, data?.max_bytes || 0)
  const map = new Map<string, number>()
  for (const c of cells) map.set(`${c.dow}-${c.hour}`, c.bytes)
  // ClickHouse toDayOfWeek -> 1=Mon..7=Sun
  const dows = [1, 2, 3, 4, 5, 6, 7]
  const dowLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  const hours = Array.from({ length: 24 }, (_, i) => i)
  const colorFor = (v: number) => {
    if (v <= 0) return 'rgba(148,163,184,0.07)'
    const t = Math.min(1, Math.sqrt(v / max))
    // warm gradient: green -> yellow -> orange -> red (proper "heat")
    const hue = 150 - 150 * t
    const sat = 75 + 15 * t
    const light = 55 - 15 * t
    return `hsl(${hue.toFixed(0)} ${sat.toFixed(0)}% ${light.toFixed(0)}%)`
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Traffic Heatmap</CardTitle>
        <p className="text-[11px] text-muted">Bytes by hour over the last 7 days</p>
      </CardHeader>
      <CardContent>
        {cells.length === 0 ? (
          <EmptyState compact />
        ) : (
          <div className="space-y-1.5">
            {dows.map((dow, di) => (
              <div key={dow} className="flex items-center gap-1.5">
                <span className="w-7 text-[10px] uppercase tracking-wider text-muted">{dowLabels[di]}</span>
                <div className="grid flex-1 grid-cols-24 gap-[3px]" style={{ gridTemplateColumns: 'repeat(24, minmax(0, 1fr))' }}>
                  {hours.map((h) => {
                    const v = map.get(`${dow}-${h}`) || 0
                    const active = activeHour === h && activeDow === dow
                    const Cell = onSelect ? 'button' : 'div'
                    return (
                      <Cell
                        key={h}
                        {...(onSelect ? { type: 'button' as const, onClick: () => onSelect(dow, h) } : {})}
                        title={`${dowLabels[di]} ${h}:00 — ${formatBytes(v)}${onSelect ? ' (click to filter)' : ''}`}
                        className={`aspect-square rounded-sm ${onSelect ? 'cursor-pointer hover:ring-2 hover:ring-primary/60' : ''} ${active ? 'ring-2 ring-primary' : ''}`}
                        style={{ background: colorFor(v) }}
                      />
                    )
                  })}
                </div>
              </div>
            ))}
            <div className="flex items-center justify-between pt-2 text-[10px] text-muted">
              <span>0h</span>
              <div className="flex items-center gap-1">
                <span>Less</span>
                <div className="flex gap-0.5">
                  {[0.1, 0.3, 0.55, 0.8, 1].map((t, i) => (
                    <span key={i} className="h-2 w-3 rounded-sm" style={{ background: colorFor(t * max) }} />
                  ))}
                </div>
                <span>More</span>
              </div>
              <span>23h</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export { TrafficHeatmap }
