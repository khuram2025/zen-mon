import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowRight, ChevronRight, Clock3, Lock, Server } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusTile } from '@/components/netflow/StatusTile'
import { ExporterStateBadge } from '@/components/netflow/ExporterStateBadge'
import { PanelMessage, QueryPanel } from '@/components/netflow/QueryPanel'
import { apiErrorMessage, apiErrorRef, httpStatus, type PanelQueryLike } from '@/lib/netflow/status'
import type { NfResult } from '@/lib/netflow/types'
import { formatBytes, formatCountExact, formatDuration, formatPercent } from '@/lib/netflow/format'
import { cn } from '@/lib/utils'
import {
  DURATION_STATUS_TEXT,
  RST_STATUS_TEXT,
  type DeviceStatusPayload,
  type ExporterHealthRow,
} from '../overviewModel'

// ─────────────────────────────────────────────────────────────────
// Exporter health (NF-01, NF-18, NF-19; formerly "Device status").
// Receipt state comes from the appliance (independent of page filters
// and of this browser's clock); a historical window shows what each
// exporter delivered in that window instead of today's state. The
// flow-derived signals say what they are measured over and why they
// are unknown when they are.
// ─────────────────────────────────────────────────────────────────

export interface ExporterHealthCardProps {
  rows: ExporterHealthRow[]
  rowsLoading?: boolean
  device: PanelQueryLike<NfResult<DeviceStatusPayload>>
  windowEndsNow: boolean
  exporterLink: (ip: string) => string
  healthLink: string
  deviceView?: boolean
  /**
   * The exporter list is unknown because its queries failed (NF-03, `exporterListUnknown`): the
   * card then shows the failure with Retry, no count and never "no exporter has sent flows".
   */
  rowsError?: { error: unknown; onRetry?: () => unknown; retrying?: boolean } | null
}

export function ExporterHealthCard({ rows, rowsLoading, device, windowEndsNow, exporterLink, healthLink, deviceView, rowsError }: ExporterHealthCardProps) {
  const estimated = !rowsError && rows.some((r) => r.mode === 'estimated')
  const forbidden = !!rowsError && httpStatus(rowsError.error) === 403
  return (
    <Card className="flex min-w-0 flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-1.5 text-sm">
          <Server className="h-4 w-4 text-text2" aria-hidden />
          Exporter health
        </CardTitle>
        <p className="text-[11px] text-text2">
          {windowEndsNow
            ? 'Receipt state measured on the appliance clock, independent of page filters'
            : 'What each exporter delivered in the selected (historical) window'}
        </p>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3">
        <QueryPanel query={device} label="exporter health signals" skeleton="tiles" compact isEmpty={() => false}>
          {(r) => <HealthTiles d={r.data} />}
        </QueryPanel>
        <div>
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="font-medium text-text">{deviceView ? 'This exporter' : rowsError || (rowsLoading && rows.length === 0) ? 'Exporters' : `Exporters (${rows.length})`}</span>
            {estimated && <span className="text-text2" title="The collector status service did not answer; states come from the last receipt time on the appliance clock">estimated</span>}
          </div>
          {rowsError ? (
            forbidden ? (
              <PanelMessage
                className="rounded border border-dashed border-border py-3"
                icon={Lock}
                title="You do not have access to the exporter list"
                detail="Your role or exporter scope does not include it."
              />
            ) : (
              <PanelMessage
                className="rounded border border-dashed border-border py-3"
                icon={AlertTriangle}
                iconClass="text-danger-fg"
                title="Could not load the exporter list"
                detail={`${apiErrorMessage(rowsError.error).replace(/\.$/, "")}. Which exporters are sending is unknown, not none.`}
                errorRef={apiErrorRef(rowsError.error)}
                onRetry={rowsError.onRetry}
                retrying={rowsError.retrying}
              />
            )
          ) : rowsLoading && rows.length === 0 ? (
            <div role="status" className="text-xs text-text2">Loading exporters…</div>
          ) : rows.length === 0 ? (
            <div className="rounded border border-dashed border-border p-2 text-center text-[11px] text-text2">
              {windowEndsNow ? 'No exporter has sent flows to this appliance' : 'No exporter delivered records in this window'}
            </div>
          ) : (
            <ul className="max-h-[220px] space-y-1.5 overflow-auto pr-1">
              {rows.map((r) => (
                <ExporterRow key={r.exporter} row={r} to={deviceView ? null : exporterLink(r.exporter)} />
              ))}
            </ul>
          )}
        </div>
        <Link to={healthLink} className="mt-auto inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline">
          Collector health <ArrowRight className="h-3 w-3" aria-hidden />
        </Link>
      </CardContent>
    </Card>
  )
}

function HealthTiles({ d }: { d: DeviceStatusPayload }) {
  const dur = d.flow_duration
  const rst = d.rst
  const cont = d.continuity
  const rstHigh = rst.pct != null && rst.pct >= 5
  return (
    <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
      <StatusTile
        tone={dur.avg_ms == null ? 'unknown' : 'neutral'}
        word={dur.avg_ms == null ? 'Unknown' : null}
        label="Average flow duration"
        value={dur.avg_ms == null ? null : formatDuration(dur.avg_ms)}
        caption={
          dur.status === 'partial' && dur.coverage_pct != null
            ? `${formatPercent(dur.coverage_pct)} of flows carry valid times`
            : DURATION_STATUS_TEXT[dur.status]
        }
        title="Bounded by the exporters' active/inactive timeouts; this is not a latency measurement"
      />
      <StatusTile
        tone={rst.pct == null ? 'unknown' : rstHigh ? 'warning' : 'neutral'}
        word={rst.pct == null ? 'Unknown' : rstHigh ? 'High' : null}
        label="TCP reset share"
        value={rst.pct == null ? null : formatPercent(rst.pct, 2)}
        caption={
          rst.pct != null
            ? `${formatCountExact(rst.rst_flows)} RST in ${formatCountExact(rst.tcp_flows_with_flags)} TCP records with exported flags${rst.status === 'partial' && rst.flag_coverage_pct != null ? ` (flags on ${formatPercent(rst.flag_coverage_pct)} of TCP)` : ''}`
            : RST_STATUS_TEXT[rst.status]
        }
        title={rst.ignored_filters.length ? `Computed over all TCP in scope; page filters not applied: ${rst.ignored_filters.join(', ')}` : 'RST records over TCP records whose flags were exported'}
      />
      <StatusTile
        tone={cont.coverage_pct == null ? 'unknown' : cont.coverage_pct < 90 ? 'warning' : 'neutral'}
        word={cont.coverage_pct == null ? 'Unknown' : cont.coverage_pct < 90 ? 'Gaps' : null}
        label="Delivery continuity"
        value={cont.coverage_pct == null ? null : formatPercent(cont.coverage_pct)}
        caption={
          cont.minutes_with_data != null
            ? `${formatCountExact(cont.minutes_with_data)} of ${formatCountExact(cont.minutes_tracked)} minutes had records`
            : 'Delivery tracking has no data for this window'
        }
        title="Minutes in which the exporters in scope delivered records (appliance receive clock)"
      />
    </div>
  )
}

function ExporterRow({ row, to }: { row: ExporterHealthRow; to: string | null }) {
  const body = (
    <>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="break-all font-mono text-[11px] font-medium text-text">{row.name}</span>
          {row.name !== row.exporter && <span className="font-mono text-[10px] text-text2">{row.exporter}</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-text2">
          {row.deviceHostname ? (
            <span>
              Device {row.deviceHostname}
              {row.deviceStatus ? ` · ${row.deviceStatus}` : ''}
            </span>
          ) : row.mapped === false ? (
            <span>Not mapped to a device</span>
          ) : null}
          {row.ageS != null && (
            <span className="inline-flex items-center gap-0.5">
              <Clock3 className="h-2.5 w-2.5" aria-hidden />
              {formatDuration(row.ageS * 1000)} ago
            </span>
          )}
          {row.bytes != null && <span>{formatBytes(row.bytes)} in window</span>}
          {row.clockSuspect && <span className="font-medium text-warning-fg">clock out of step</span>}
        </div>
      </div>
      <div className="shrink-0 text-right">
        {row.mode === 'historical' ? (
          <span className="text-[11px] tabular-nums text-text" title="Share of the window's minutes in which this exporter delivered records">
            {row.coveragePct == null ? '—' : `${formatPercent(row.coveragePct)} of minutes`}
          </span>
        ) : row.state ? (
          <ExporterStateBadge state={row.state} />
        ) : null}
      </div>
      {to && <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text2" aria-hidden />}
    </>
  )
  const cls = 'flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5'
  return (
    <li>
      {to ? (
        <Link to={to} className={cn(cls, 'transition-colors hover:border-primary/50 hover:bg-primary/5')}>
          {body}
        </Link>
      ) : (
        <div className={cls}>{body}</div>
      )}
    </li>
  )
}
