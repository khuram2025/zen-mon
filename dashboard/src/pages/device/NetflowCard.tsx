import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowRight, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { buildNetflowLink } from '@/lib/netflow/links'
import { useCan } from '@/stores/auth'
import { SERIES_COLORS, TimeSeriesPanel, Panel } from './charts'
import { formatBitRate, formatBitRateAxis, formatBytes, formatRelative, MISSING } from './format'
import type { DeviceRange } from './types'

type NetflowOverview = {
  bytes: number; packets: number; flows: number; exporters: number; src_hosts: number; dst_hosts: number
  last_seen: string | null; current_bps: number
}
type NetflowSeriesPoint = { ts: number; bps: number }
type NetflowApplication = { name: string; bytes: number; flows: number; share_pct?: number }

export function normalizeIp(ip: string | null | undefined): string {
  return String(ip || '').split('/')[0].trim()
}

/** The exporter's NetFlow page for the same window the device page shows. */
function netflowDeviceLink(exporterIp: string, range: DeviceRange): string {
  const rolling: Record<number, string> = { 1: '1h', 24: '24h', 168: '7d', 720: '1M' }
  const key = range.isCustom ? null : rolling[Math.round(range.hours)]
  const extra = key ? { range: key } : { range: 'custom', from: range.fromISO, to: range.toISO }
  return buildNetflowLink(`/netflow/devices/${encodeURIComponent(exporterIp)}`, { extra })
}

function useNetflowQS(exporter: string, range: DeviceRange) {
  return useMemo(() => {
    const params = new URLSearchParams({ hours: String(Math.max(1, Math.round(range.hours))) })
    params.set('exporter', exporter)
    if (range.isCustom) { params.set('from', range.fromISO); params.set('to', range.toISO) }
    return params.toString()
  }, [exporter, range.hours, range.isCustom, range.fromISO, range.toISO])
}

/** NetFlow overview for an exporting device. Needs `netflow.view`; without it
 *  nothing is requested and the card stays hidden, as for a device that exports no flows. */
export function useDeviceNetflow(deviceId: string, ip: string | null | undefined, range: DeviceRange) {
  const canNetflow = useCan()('netflow.view')
  const exporter = normalizeIp(ip)
  const qs = useNetflowQS(exporter, range)
  const overview = useQuery<NetflowOverview>({
    queryKey: ['device', deviceId, 'netflow-overview', qs],
    queryFn: async () => (await api.get(`/netflow/overview?${qs}`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    placeholderData: keepPreviousData,
    enabled: !!exporter && canNetflow,
  })
  return { exporter, qs, overview: overview.data, hasNetflow: canNetflow && (overview.data?.flows || 0) > 0 }
}

export function DeviceNetflowCard({ deviceId, exporter, qs, overview, range }: {
  deviceId: string; exporter: string; qs: string; overview: NetflowOverview; range: DeviceRange
}) {
  const { data: series } = useQuery<NetflowSeriesPoint[]>({
    queryKey: ['device', deviceId, 'netflow-timeseries', qs],
    queryFn: async () => (await api.get(`/netflow/timeseries?${qs}`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    placeholderData: keepPreviousData,
  })
  const { data: applications } = useQuery<NetflowApplication[]>({
    queryKey: ['device', deviceId, 'netflow-applications', qs],
    queryFn: async () => (await api.get(`/netflow/applications?${qs}`)).data,
    refetchInterval: range.isCustom ? false : 60_000,
    placeholderData: keepPreviousData,
  })
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const apps = applications || []
  const top = apps[0]
  // One application carrying every byte of a busy exporter is a classification
  // problem far more often than real traffic (one application ID on every
  // flow, or a catch-all rule). Say so instead of presenting it as a finding.
  const singleApp = !!top && overview.bytes > 0 && (apps.length === 1 || top.bytes >= overview.bytes * 0.999)
  return (
    <Panel
      title="NetFlow"
      meta={<>Exporter <span className="font-mono">{exporter}</span> · {range.label} · last flow {formatRelative(overview.last_seen)}</>}
      actions={<Link to={netflowDeviceLink(exporter, range)} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">Open NetFlow<ArrowRight className="h-3 w-3" /></Link>}
    >
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(260px,0.7fr)]">
        <div className="min-w-0">
          <dl className="mb-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div><dt className="text-xs text-muted">Current rate</dt><dd className="font-semibold tabular-nums">{overview.current_bps > 0 ? formatBitRate(overview.current_bps) : 'Idle'}</dd></div>
            <div><dt className="text-xs text-muted">Volume</dt><dd className="font-semibold tabular-nums">{formatBytes(overview.bytes)}</dd></div>
            <div><dt className="text-xs text-muted">Flows</dt><dd className="font-semibold tabular-nums">{overview.flows.toLocaleString()}</dd></div>
            <div><dt className="text-xs text-muted">Hosts</dt><dd className="font-semibold tabular-nums">{overview.src_hosts.toLocaleString()} → {overview.dst_hosts.toLocaleString()}</dd></div>
          </dl>
          <TimeSeriesPanel height={170} fromTs={fromTs} toTs={toTs} rangeHours={range.hours} domain="auto"
            format={formatBitRate} axisFormat={(v) => `${formatBitRateAxis(v)}bps`}
            series={[{ key: 'bps', name: 'Throughput', color: SERIES_COLORS.primary, points: (series || []).map((p) => ({ ts: p.ts, value: p.bps })) }]}
            emptyText="No flow series in this period; totals above cover the whole window" />
        </div>
        <div className="space-y-2 text-sm">
          <div className="text-xs font-medium text-muted">Top applications</div>
          {singleApp ? (
            <div className="rounded-lg border border-warning/40 bg-warning/5 p-3">
              <div className="font-medium">{top.name} · {formatBytes(top.bytes)}</div>
              <p className="mt-1 flex gap-1.5 text-xs text-muted"><Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />Every flow from this exporter is classified as one application. Check the exporter’s application IDs or the NetFlow application rules before relying on this breakdown.</p>
            </div>
          ) : apps.length ? (
            <ol className="space-y-1.5">
              {apps.slice(0, 5).map((a) => {
                const share = overview.bytes > 0 ? (a.bytes / overview.bytes) * 100 : null
                return (
                  <li key={a.name}>
                    <div className="flex items-baseline justify-between gap-2"><span className="min-w-0 break-words">{a.name}</span><span className="shrink-0 tabular-nums text-muted">{formatBytes(a.bytes)}{share != null ? ` · ${share < 1 ? '<1' : Math.round(share)}%` : ''}</span></div>
                    <div className="mt-0.5 h-1.5 overflow-hidden rounded bg-surface2"><div className="h-full bg-primary" style={{ width: `${Math.min(100, share ?? 0)}%` }} /></div>
                  </li>
                )
              })}
            </ol>
          ) : <p className="text-muted">{applications ? 'No application data' : MISSING}</p>}
        </div>
      </div>
    </Panel>
  )
}
