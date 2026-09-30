import { Cable, Info, Loader2 } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { ExporterDevice, Interface } from '../types'
import { interfaceLabel, formatBitsPerSecond } from '../model'
import { EmptyState } from './EmptyState'

function TopInterfacesCard({
  interfaces,
  loading,
  activeIface,
  onSelect,
  showExporter,
  unmappedExporters = [],
  onMapExporters,
  compact = false,
}: {
  interfaces: Interface[]
  loading: boolean
  activeIface: number | null
  onSelect: (val: number | null) => void
  showExporter: boolean
  unmappedExporters?: ExporterDevice[]
  onMapExporters?: () => void
  compact?: boolean
}) {
  const max = Math.max(1, ...interfaces.map((i) => i.bytes))
  // Only nag when an unresolved exporter is actually represented in this list.
  const unresolvedHere = unmappedExporters.filter((e) =>
    interfaces.some((i) => i.exporter_ip === e.exporter_ip),
  )
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <Cable className="h-4 w-4 text-primary" />
          <div>
            <CardTitle>Top Interfaces</CardTitle>
            <p className="text-xs text-muted">
              {showExporter ? 'Highest-traffic interfaces across all exporters' : 'Highest-traffic interfaces on this device'} · click a row to filter
            </p>
          </div>
        </div>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
      </CardHeader>
      <CardContent>
        {unresolvedHere.length > 0 && onMapExporters && (
          <button
            onClick={onMapExporters}
            className="mb-2 flex w-full items-start gap-2 rounded-md border border-border bg-surface2/50 px-2.5 py-2 text-left text-[11px] text-muted transition-colors hover:border-primary/40 hover:text-text"
          >
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
            <span>
              {unresolvedHere.map((e) => e.exporter_ip).join(', ')} export
              {unresolvedHere.length === 1 ? 's' : ''} from an address that is not a monitored device's
              management IP, so its interfaces show raw ifIndex numbers.{' '}
              <span className="font-medium text-primary">Link it to a device →</span>
            </span>
          </button>
        )}
        {interfaces.length === 0 ? (
          <EmptyState compact />
        ) : (
          <div className={compact ? 'flex flex-col gap-1.5' : 'grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5'}>
            {interfaces.map((it, idx) => {
              const active = activeIface === it.ifindex
              const widthPct = Math.max(4, (it.bytes / max) * 100)
              if (compact) {
                // Single-row condensed list item: index | name | thin bar | bytes
                return (
                  <button
                    key={`${it.exporter_ip}-${it.ifindex}-${idx}`}
                    onClick={() => onSelect(active ? null : it.ifindex)}
                    className={`group relative flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors ${
                      active
                        ? 'border-primary/60 bg-primary/10 ring-1 ring-primary/40'
                        : 'border-border bg-surface2/40 hover:border-primary/40 hover:bg-primary/5'
                    }`}
                    title={it.if_alias || ''}
                  >
                    <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold ${active ? 'bg-primary text-black' : 'bg-primary/15 text-primary'}`}>
                      {idx + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-xs font-semibold">{interfaceLabel(it)}</span>
                        <span className="shrink-0 text-[10px] font-medium text-muted">{formatBytes(it.bytes)}</span>
                      </div>
                      <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface">
                        <div className="h-full rounded-full bg-primary" style={{ width: `${widthPct}%` }} />
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-1 text-[9px] text-muted">
                        <span className="truncate font-mono">
                          {it.device_hostname || it.exporter_ip}
                          {it.if_alias ? ` · ${it.if_alias}` : ''}
                        </span>
                        <span className="shrink-0">
                          {it.if_speed && it.if_speed > 0 ? formatBitsPerSecond(it.if_speed) : `${it.flows.toLocaleString()} flows`}
                        </span>
                      </div>
                    </div>
                  </button>
                )
              }
              const inPct = it.bytes ? (it.in_bytes / it.bytes) * 100 : 0
              return (
                <button
                  key={`${it.exporter_ip}-${it.ifindex}-${idx}`}
                  onClick={() => onSelect(active ? null : it.ifindex)}
                  className={`group flex flex-col items-stretch rounded-md border p-3 text-left transition-colors ${
                    active
                      ? 'border-primary/60 bg-primary/10 ring-1 ring-primary/40'
                      : 'border-border bg-surface2/40 hover:border-primary/40 hover:bg-primary/5'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${active ? 'bg-primary text-black' : 'bg-primary/15 text-primary'}`}>
                        {idx + 1}
                      </span>
                      <span className="truncate text-sm font-semibold" title={it.if_alias || it.if_descr || ''}>
                        {interfaceLabel(it)}
                      </span>
                    </div>
                    <span className="shrink-0 text-[11px] font-medium text-muted">{formatBytes(it.bytes)}</span>
                  </div>
                  {it.if_alias && (
                    <div className="mt-1 truncate text-[10px] text-muted" title={it.if_alias}>{it.if_alias}</div>
                  )}
                  {showExporter && (
                    <div className="mt-1 truncate font-mono text-[10px] text-muted">
                      {it.device_hostname ? `${it.device_hostname} · ` : ''}{it.exporter_ip} · index {it.ifindex}
                    </div>
                  )}
                  {it.if_speed && it.if_speed > 0 && (
                    <div className="mt-1 flex items-center justify-between text-[10px]">
                      <span className="text-muted">Capacity</span>
                      <span className="text-muted">{formatBitsPerSecond(it.if_speed)}</span>
                    </div>
                  )}
                  <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface">
                    <div className="h-full rounded-full bg-primary" style={{ width: `${widthPct}%` }} />
                  </div>
                  <div className="mt-2 flex items-center justify-between text-[10px] text-muted">
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-cyan-400" />
                      In {formatBytes(it.in_bytes)} ({inPct.toFixed(0)}%)
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-violet-400" />
                      Out {formatBytes(it.out_bytes)}
                    </span>
                  </div>
                  <div className="mt-1 text-[10px] text-muted">
                    {it.flows.toLocaleString()} flows · {it.packets.toLocaleString()} pkts
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export { TopInterfacesCard }
