import { useState } from 'react'
import { Server } from 'lucide-react'
import { useServerNowTick } from '@/components/services/detail/clock'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { useNfQuery } from '@/lib/netflow/api'
import type { NfResult } from '@/lib/netflow/types'
import { formatBytes, formatCount, formatCountExact, formatDateTime } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import type { UseQueryResult } from '@tanstack/react-query'
import { SeriesChart } from './SeriesChart'
import { collectorTone, collectorWord, droppedRecords, formatAge, type CollectorHealth } from './model'
import { Metric, Segmented, ToneChip } from './ui'

export const COLLECTOR_WINDOWS = [
  { value: '1', label: '1 h' },
  { value: '24', label: '24 h' },
  { value: '168', label: '7 d' },
  { value: '720', label: '30 d' },
] as const
export type CollectorWindow = (typeof COLLECTOR_WINDOWS)[number]['value']

export function useCollectorHealth(hours: CollectorWindow) {
  return useNfQuery<CollectorHealth>('/netflow/collector/health', { hours, tz: displayTimeZone() }, { refetchInterval: hours === '1' ? 30_000 : 120_000 })
}

/**
 * The collector process (live health endpoint) and its persisted statistics (FE-01): what came
 * in, what was decoded and stored, and every reason something was not.
 */
export function CollectorPanel({ hours, onHoursChange, query }: {
  hours: CollectorWindow
  onHoursChange: (h: CollectorWindow) => void
  query: UseQueryResult<NfResult<CollectorHealth>>
}) {
  return (
    <Card>
      <CardHeader className="flex flex-col gap-2 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Server className="h-4 w-4 text-primary" aria-hidden /> Collector
        </CardTitle>
        <Segmented label="Statistics period" value={hours} onChange={onHoursChange}
          options={COLLECTOR_WINDOWS.map((w) => ({ value: w.value, label: w.label }))} />
      </CardHeader>
      <CardContent className="space-y-4">
        <QueryPanel query={query} label="collector health" skeleton="tiles">
          {(r) => <CollectorBody c={r.data} />}
        </QueryPanel>
        {query.data && <DataQualityBar meta={query.data.meta} coverage={null} />}
      </CardContent>
    </Card>
  )
}

function CollectorBody({ c }: { c: CollectorHealth }) {
  const tz = displayTimeZone()
  const now = useServerNowTick(10_000)
  const live = c.live
  const t = c.stats?.totals
  const p = c.stats?.process
  const dropped = droppedRecords(t)
  const lastOk = live.delivery?.last_insert_ok_at ? Date.parse(live.delivery.last_insert_ok_at) : NaN
  const statsAge = p?.last_stats_at ? (now - Date.parse(p.last_stats_at)) / 1000 : null
  const [showCounters, setShowCounters] = useState(false)
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
        <StatusTile
          tone={collectorTone(live.status, live.reachable)}
          word={collectorWord(live.status, live.reachable)}
          label="Collector process"
          value={live.reachable ? (live.listening ? `Listening ${live.listen ?? ''}`.trim() : 'Not listening') : 'No answer'}
          caption={live.reachable
            ? `${live.collector_id ?? 'collector'} · up ${formatAge(live.uptime_s)} · clock policy ${live.clock_policy ?? '—'}`
            : `The health endpoint did not answer (${live.error ?? 'error'}); figures below come from stored statistics`}
        />
        <StatusTile
          tone={!t ? 'unknown' : dropped > 0 ? 'warning' : 'neutral'}
          word={!t ? 'Unknown' : dropped > 0 ? 'Drops' : null}
          label="Records stored"
          value={t ? formatCount(t.records_written) : null}
          title={t ? `${formatCountExact(t.records_written)} stored of ${formatCountExact(t.records_decoded)} decoded` : undefined}
          caption={t ? `of ${formatCount(t.records_decoded)} decoded · ${formatCount(dropped)} dropped` : 'Statistics unavailable'}
        />
        <StatusTile
          tone={!p ? 'unknown' : p.insert_failures > 0 ? 'warning' : 'success'}
          word={!p ? 'Unknown' : p.insert_failures > 0 ? 'Retries' : 'OK'}
          label="Database delivery"
          value={Number.isFinite(lastOk) ? `${formatAge((now - lastOk) / 1000)} ago` : '—'}
          caption={p ? `${formatCount(p.insert_failures)} failed inserts, ${formatCount(p.insert_retries)} retries in the period` : 'Last successful insert'}
        />
        <StatusTile
          tone={!p ? 'unknown' : p.kernel_rcvbuf_errors > 0 || (live.socket_drops ?? 0) > 0 ? 'warning' : 'success'}
          word={!p ? 'Unknown' : p.kernel_rcvbuf_errors > 0 ? 'Drops' : 'OK'}
          label="Kernel receive drops"
          value={p ? formatCount(p.kernel_rcvbuf_errors) : null}
          caption={`Datagrams the OS dropped before the collector read them${live.rcvbuf_bytes ? ` · buffer ${formatBytes(live.rcvbuf_bytes)}` : ''}`}
        />
      </div>

      {live.reachable && live.reasons.length > 0 && (
        <ul className="space-y-1" aria-label="Collector status reasons">
          {live.reasons.map((r, i) => (
            <li key={i} className="flex items-start gap-2 text-xs text-text">
              <ToneChip tone={live.status === 'down' ? 'danger' : 'warning'}>{live.status === 'down' ? 'Down' : 'Degraded'}</ToneChip>
              <span className="pt-0.5">{r}</span>
            </li>
          ))}
        </ul>
      )}

      {t && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="Datagrams" value={formatCount(t.datagrams)} title={formatCountExact(t.datagrams)} hint={formatBytes(t.datagram_bytes)} />
          <Metric label="Exporters sending" value={formatCount(t.exporters)} />
          <Metric label="Waiting for template" value={formatCount(t.data_sets_waiting_template)} hint="data sets" />
          <Metric label="Parse errors" value={formatCount(t.parse_errors)} />
          <Metric label="Sequence gaps" value={formatCount(t.sequence_gaps)} hint="lost packets / records" />
          <Metric label="Clock-clamped" value={formatCount(t.clock_clamped_records)} hint="records re-timed" />
          {(t.sampling_ambiguous_records ?? 0) > 0 && (
            <Metric
              label="Sampling unknown"
              value={formatCount(t.sampling_ambiguous_records ?? 0)}
              hint="records stored 1 in 1"
              title="These records named no sampler while their exporter announced samplers with different rates, so no rate was applied"
            />
          )}
          <Metric label="Dropped: queue full" value={formatCount(t.records_dropped_queue)} />
          <Metric label="Dropped: implausible" value={formatCount(t.records_dropped_implausible)} />
          <Metric label="Dropped: unknown exporter" value={formatCount(t.records_dropped_unknown_exporter)} />
          <Metric label="Unsupported records" value={formatCount(t.records_unsupported)} />
          <Metric
            label="Queue"
            value={live.queue ? `${formatCount(live.queue.queue_len)} / ${formatCount(live.queue.queue_cap)}` : p ? formatCount(p.last_queue_len) : '—'}
            hint={p ? `peak ${formatCount(p.max_queue_len)} in the period` : undefined}
          />
          <Metric
            label="Disk spool"
            value={live.queue ? (live.queue.spool_enabled ? formatCount(live.queue.spool_records) : 'Off') : p ? formatCount(p.last_spool_records) : '—'}
            hint={p ? `peak ${formatCount(p.max_spool_records)} records` : undefined}
          />
        </div>
      )}

      {c.series && (
        <SeriesChart series={c.series} from={c.window.from} to={c.window.to} granularityS={c.window.granularity_s} title="Records stored" />
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text2">
        {p?.last_stats_at && (
          <span>
            Statistics written {formatDateTime(p.last_stats_at, tz, { seconds: false })}
            {statsAge != null && statsAge > 180 && <> — <span className="font-semibold text-warning-fg">{formatAge(statsAge)} ago: the collector may be stopped</span></>}
          </span>
        )}
        {c.stats?.collectors.map((k) => <span key={k.collector_id}>Collector {k.collector_id}</span>)}
        {live.counters && (
          <button type="button" className="font-medium text-text underline-offset-2 hover:underline" onClick={() => setShowCounters((v) => !v)} aria-expanded={showCounters}>
            {showCounters ? 'Hide' : 'Show'} counters since the collector started
          </button>
        )}
      </div>
      {showCounters && live.counters && (
        <dl className="grid grid-cols-1 gap-x-6 gap-y-1 rounded-md border border-border p-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(live.counters).map(([k, v]) => (
            <div key={k} className="flex justify-between gap-2">
              <dt className="text-text2">{k.replace(/_/g, ' ')}</dt>
              <dd className="tabular-nums text-text">{formatCountExact(v as number)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
