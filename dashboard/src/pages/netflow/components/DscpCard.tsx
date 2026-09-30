import { Shield } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { DscpClass } from '../types'
import { DSCP_COLORS } from '../model'
import { EmptyState } from './EmptyState'

function DscpCard({ data, activeDscp, onSelect }: { data: DscpClass[]; activeDscp?: number | null; onSelect?: (dscp: number) => void }) {
  const top = data.slice(0, 6)
  const total = top.reduce((a, b) => a + b.bytes, 0)
  const max = Math.max(1, ...top.map((d) => d.bytes))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Shield className="h-4 w-4 text-cyan-400" />
        <div>
          <CardTitle className="text-sm">DSCP / QoS Classes</CardTitle>
          <p className="text-[11px] text-muted">Click a class to filter the page</p>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {top.length === 0 && <EmptyState compact />}
        {top.map((d, i) => {
          const active = activeDscp === d.dscp
          return (
            <button
              key={d.dscp}
              type="button"
              onClick={() => onSelect?.(d.dscp)}
              className={`block w-full space-y-0.5 rounded px-1 py-1 text-left transition-colors hover:bg-primary/5 ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
            >
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex min-w-0 items-center gap-1.5 truncate">
                  <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: DSCP_COLORS[i % DSCP_COLORS.length] }} />
                  <span className="truncate font-medium">{d.label}</span>
                </span>
                <span className="shrink-0 text-muted">{formatBytes(d.bytes)} · {total ? ((d.bytes / total) * 100).toFixed(1) : 0}%</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-surface">
                <div className="h-full rounded-full" style={{ width: `${(d.bytes / max) * 100}%`, background: DSCP_COLORS[i % DSCP_COLORS.length] }} />
              </div>
            </button>
          )
        })}
      </CardContent>
    </Card>
  )
}

export { DscpCard }
