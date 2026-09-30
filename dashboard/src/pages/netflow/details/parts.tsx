import { formatBytes } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import type { NetflowInterfaceRef } from '../types'
import { interfaceLabel, describePortClass } from '../model'

function DetailMetric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums text-text">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted">{sub}</div>
    </div>
  )
}

function DetailListPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</div>
      <div className="space-y-3">{children}</div>
    </div>
  )
}

function PillList({ label, values, empty, mono = false }: { label: string; values: string[]; empty: string; mono?: boolean }) {
  const clean = Array.from(new Set(values.filter(Boolean))).slice(0, 8)
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-muted">{label}</div>
      {clean.length ? (
        <div className="flex flex-wrap gap-1.5">
          {clean.map((value) => (
            <span key={value} className={`rounded border border-border bg-surface px-2 py-1 text-[11px] text-text ${mono ? 'font-mono' : ''}`}>
              {value}
            </span>
          ))}
        </div>
      ) : (
        <div className="text-xs text-muted">{empty}</div>
      )}
    </div>
  )
}

function PortBreakdown({ ports, totalBytes }: { ports: any[]; totalBytes: number }) {
  const clean = ports.slice(0, 8)
  if (!clean.length) {
    return <div className="rounded-md border border-dashed border-border bg-surface2/30 p-5 text-center text-xs text-muted">No TCP/UDP destination port detail for this item.</div>
  }
  const max = Math.max(1, ...clean.map((p) => Number(p.bytes || 0)), totalBytes || 0)
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-3 flex items-center justify-between text-[11px]">
        <span className="font-semibold uppercase tracking-wider text-muted">Top Services / Ports</span>
        <span className="text-muted">{formatBytes(totalBytes || clean.reduce((sum, p) => sum + Number(p.bytes || 0), 0))} total</span>
      </div>
      <div className="space-y-2">
        {clean.map((port) => {
          const bytes = Number(port.bytes || 0)
          return (
            <div key={`${port.port}-${port.service}`}>
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex min-w-0 items-center gap-1.5">
                  <Badge variant="outline">{port.service || `Port ${port.port}`}</Badge>
                  <span className="truncate text-muted">{port.application || describePortClass(Number(port.port || 0))}</span>
                </span>
                <span className="shrink-0 font-medium text-text">{formatBytes(bytes)} · {Number(port.flows || 0).toLocaleString()} flows</span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
                <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(4, (bytes / max) * 100)}%` }} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PortBadge({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-surface px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-0.5 truncate font-mono text-xs font-semibold text-text">{value}</div>
    </div>
  )
}

function TrafficShapeBar({ label, value, max, display, color }: { label: string; value: number; max: number; display: string; color: string }) {
  const width = Math.max(6, Math.min(100, (value / Math.max(1, max)) * 100))
  return (
    <div>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span className="text-muted">{label}</span>
        <span className="font-medium text-text">{display}</span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-surface">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

function InterfaceChipGroup({ title, values, empty }: { title: string; values: NetflowInterfaceRef[]; empty: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</div>
      {values.length ? (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <span key={`${value.exporter_ip}-${value.ifindex}`} className="rounded border border-border bg-surface px-2 py-1 text-[11px] text-text">
              <span className="font-medium">{interfaceLabel(value)}</span>
              <span className="ml-1 font-mono text-muted">#{value.ifindex}</span>
            </span>
          ))}
        </div>
      ) : (
        <div className="text-xs text-muted">{empty}</div>
      )}
    </div>
  )
}

export { DetailMetric, DetailListPanel, PillList, PortBreakdown, PortBadge, TrafficShapeBar, InterfaceChipGroup }
