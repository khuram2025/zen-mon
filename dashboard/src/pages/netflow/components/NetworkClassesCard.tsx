import { Network } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { NetClass } from '../types'
import { NETCLASS_COLORS } from '../model'
import { EmptyState } from './EmptyState'

function NetworkClassesCard({ data, activeClass, onSelect }: { data: NetClass[]; activeClass?: string | null; onSelect?: (name: string) => void }) {
  const total = Math.max(1, data.reduce((a, b) => a + b.bytes, 0))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Network className="h-4 w-4 text-emerald-400" />
        <div>
          <CardTitle className="text-sm">Network Class Mix</CardTitle>
          <p className="text-[11px] text-muted">Private vs public traffic distribution</p>
        </div>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <EmptyState compact />
        ) : (
          <>
            {/* Stacked bar */}
            <div className="flex h-2 overflow-hidden rounded-full bg-surface">
              {data.map((d) => (
                <div
                  key={d.name}
                  title={`${d.name}: ${formatBytes(d.bytes)} (${((d.bytes / total) * 100).toFixed(1)}%)`}
                  style={{ width: `${(d.bytes / total) * 100}%`, background: NETCLASS_COLORS[d.name] || '#64748b' }}
                />
              ))}
            </div>
            <div className="mt-3 space-y-1 text-[11px]">
              {data.map((d) => {
                const active = activeClass === d.name
                return (
                  <button
                    key={d.name}
                    type="button"
                    onClick={() => onSelect?.(d.name)}
                    className={`flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left transition-colors hover:bg-primary/5 ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-sm" style={{ background: NETCLASS_COLORS[d.name] || '#64748b' }} />
                      <span>{d.name}</span>
                    </span>
                    <span className="text-muted">{formatBytes(d.bytes)} · {((d.bytes / total) * 100).toFixed(1)}%</span>
                  </button>
                )
              })}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

export { NetworkClassesCard }
