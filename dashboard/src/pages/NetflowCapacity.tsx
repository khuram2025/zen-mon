import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, Gauge, TrendingUp } from 'lucide-react'
import { api } from '@/lib/api'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'

type CapacityRow = {
  exporter_ip: string
  ifindex: number
  if_name: string | null
  if_alias: string | null
  if_speed: number | null
  device_hostname: string | null
  p95_bps: number
  avg_bps: number
  max_bps: number
  utilization_p95_pct: number | null
  utilization_max_pct: number | null
  total_bytes: number
  total_packets: number
}

function formatBps(bps: number): string {
  const u = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let v = bps; let i = 0
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`
}

export function NetflowCapacityPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const qs = new URLSearchParams({ hours: String(range.hours), limit: '50' })
  if (isCustom) { qs.set('from', range.fromISO); qs.set('to', range.toISO) }

  const capacity = useQuery<CapacityRow[]>({
    queryKey: ['netflow', 'capacity', qs.toString()],
    queryFn: async () => (await api.get(`/netflow/capacity?${qs.toString()}`)).data,
    refetchInterval: isCustom ? false : 60_000,
  })
  const rows = capacity.data || []

  // Bucket interfaces by utilization band for the summary cards.
  const utilBuckets = { critical: 0, warning: 0, healthy: 0, unknown: 0 }
  for (const r of rows) {
    if (r.utilization_p95_pct === null) utilBuckets.unknown++
    else if (r.utilization_p95_pct >= 70) utilBuckets.critical++
    else if (r.utilization_p95_pct >= 40) utilBuckets.warning++
    else utilBuckets.healthy++
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
            <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
              <ArrowLeft className="h-3 w-3" />
              NetFlow
            </Link>
            <ChevronRight className="h-3 w-3" />
            <span>Capacity Planning</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <TrendingUp className="h-5 w-5 text-primary" />
            Capacity Planning
          </h1>
          <p className="text-xs text-muted">95th-percentile utilization vs SNMP ifSpeed — billing &amp; growth view.</p>
        </div>
        <TimeRangePicker
          rangeIdx={rangeIdx}
          isCustom={isCustom}
          customFrom={range.fromISO}
          customTo={range.toISO}
          onPreset={setPreset}
          onCustom={setCustom}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Summary label="Critical (≥70%)" value={utilBuckets.critical} tone="rose" />
        <Summary label="Warning (40-70%)" value={utilBuckets.warning} tone="amber" />
        <Summary label="Healthy (<40%)" value={utilBuckets.healthy} tone="emerald" />
        <Summary label="Unknown speed" value={utilBuckets.unknown} tone="slate" />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Interfaces by 95th-percentile</CardTitle>
          <p className="text-[11px] text-muted">Industry-standard 95p over 5-minute slots; billing benchmarks usually quote this number.</p>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-surface2/30 p-10 text-center text-xs text-muted">
              No interface traffic in this window.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Interface</Th>
                  <Th>Device</Th>
                  <Th className="text-right">Capacity</Th>
                  <Th className="text-right">Avg</Th>
                  <Th className="text-right">95p</Th>
                  <Th className="text-right">Max</Th>
                  <Th className="text-right">Util (95p)</Th>
                  <Th>Headroom</Th>
                </Tr>
              </THead>
              <TBody>
                {rows.map((r) => {
                  const util = r.utilization_p95_pct
                  const tone = util === null ? 'text-muted' : util >= 70 ? 'text-rose-400' : util >= 40 ? 'text-amber-400' : 'text-emerald-400'
                  const widthPct = Math.min(100, Math.max(2, util ?? 0))
                  return (
                    <Tr key={`${r.exporter_ip}-${r.ifindex}`}>
                      <Td>
                        <div className="text-sm font-semibold">{r.if_name || `if ${r.ifindex}`}</div>
                        {r.if_alias && <div className="text-[10px] text-muted">{r.if_alias}</div>}
                      </Td>
                      <Td>
                        <div className="text-xs">{r.device_hostname || '—'}</div>
                        <div className="font-mono text-[10px] text-muted">{r.exporter_ip} · ifIndex {r.ifindex}</div>
                      </Td>
                      <Td className="text-right text-xs">{r.if_speed ? formatBps(r.if_speed) : '—'}</Td>
                      <Td className="text-right text-xs">{formatBps(r.avg_bps)}</Td>
                      <Td className="text-right text-sm font-semibold">{formatBps(r.p95_bps)}</Td>
                      <Td className="text-right text-xs text-muted">{formatBps(r.max_bps)}</Td>
                      <Td className={`text-right text-sm font-semibold ${tone}`}>
                        {util !== null ? `${util.toFixed(2)}%` : '—'}
                      </Td>
                      <Td>
                        {util !== null ? (
                          <div className="h-1.5 w-32 overflow-hidden rounded-full bg-surface">
                            <div
                              className={`h-full rounded-full ${util >= 70 ? 'bg-rose-500' : util >= 40 ? 'bg-amber-400' : 'bg-emerald-400'}`}
                              style={{ width: `${widthPct}%` }}
                            />
                          </div>
                        ) : (
                          <span className="text-[10px] text-muted">no ifSpeed</span>
                        )}
                      </Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Summary({ label, value, tone }: { label: string; value: number; tone: 'rose' | 'amber' | 'emerald' | 'slate' }) {
  const map = {
    rose: 'border-rose-500/30 bg-rose-500/5 text-rose-300',
    amber: 'border-amber-400/30 bg-amber-500/5 text-amber-300',
    emerald: 'border-emerald-400/30 bg-emerald-500/5 text-emerald-300',
    slate: 'border-border bg-surface2/30 text-muted',
  }
  return (
    <div className={`rounded-md border p-3 ${map[tone]}`}>
      <div className="flex items-center gap-1.5">
        <Gauge className="h-3.5 w-3.5" />
        <span className="text-[10px] uppercase tracking-wider opacity-80">{label}</span>
      </div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      <div className="text-[10px] opacity-70">interfaces</div>
    </div>
  )
}
