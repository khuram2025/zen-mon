import { Link } from 'react-router-dom'
import { ChevronRight, Database, Gauge } from 'lucide-react'
import { relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { Exporter, DeviceStatus } from '../types'
import { MiniStat } from './MiniStat'

// ─────────────────────────────────────────────────────────────────
// Device Status (Latency / Packet Loss / Uptime)
// ─────────────────────────────────────────────────────────────────

function DeviceStatusCard({ status, exporters, deviceView }: { status?: DeviceStatus; exporters: Exporter[]; deviceView?: boolean }) {
  // These are flow-derived heuristics (avg flow duration, TCP RST ratio) —
  // NOT real latency / packet-loss measurements. Label them honestly.
  const flowDuration = status?.latency_ms ?? 0
  const rstRatio = status?.packet_loss_pct ?? 0
  return (
    <Card className="shrink-0">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Exporter Health</CardTitle>
        <p className="text-[11px] text-muted">Flow-derived signals (not ICMP/SNMP probes)</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <MiniStat icon={Gauge} label="Avg Flow Duration" value={`${flowDuration} ms`} tone="cyan" />
          <MiniStat icon={Database} label="TCP RST Ratio" value={`${rstRatio.toFixed(2)}%`} tone={rstRatio >= 5 ? 'rose' : 'emerald'} />
        </div>
        <div className="rounded-md border border-border bg-surface2/30 p-2.5">
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="font-medium text-text">Exporting Devices</span>
            <span className="text-muted">{exporters.length}</span>
          </div>
          <div className="max-h-[180px] space-y-1.5 overflow-auto pr-1">
            {exporters.length === 0 && (
              <div className="rounded border border-dashed border-border bg-surface/40 p-2 text-center text-[10px] text-muted">
                No exporters in range
              </div>
            )}
            {exporters.map((e) => {
              const age = Date.now() - new Date(e.last_seen).getTime()
              const tone =
                age < 2 * 60_000
                  ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]'
                  : age < 15 * 60_000
                  ? 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.7)]'
                  : 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.7)]'
              const Row = deviceView ? 'div' : Link
              const rowProps = deviceView
                ? { className: 'flex items-center gap-2 rounded border border-border/60 bg-surface/50 px-2 py-1.5' }
                : { to: `/netflow/devices/${encodeURIComponent(e.exporter_ip)}`, className: 'flex items-center gap-2 rounded border border-border/60 bg-surface/50 px-2 py-1.5 transition-colors hover:border-primary/60 hover:bg-primary/5' }
              return (
                <Row key={e.exporter_ip} {...(rowProps as any)}>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone}`} />
                  <span className="truncate font-mono text-[10px]">{e.exporter_ip}</span>
                  <span className="ml-auto shrink-0 text-[9px] text-muted" title={e.last_seen}>
                    {relativeTime(e.last_seen)}
                  </span>
                  {!deviceView && <ChevronRight className="h-3 w-3 shrink-0 text-muted" />}
                </Row>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export { DeviceStatusCard }
