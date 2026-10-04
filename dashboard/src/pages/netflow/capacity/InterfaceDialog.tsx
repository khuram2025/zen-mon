import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { BarChart3, Cable, ExternalLink, Gauge, LineChart as LineChartIcon, Search, Table2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { cn } from '@/lib/utils'
import { nfParams, useNfQuery } from '@/lib/netflow/api'
import { formatBps, formatBpsAxis, formatCount, formatDateTime, formatPercent, ipDisplay } from '@/lib/netflow/format'
import type { FlowFilter } from '@/lib/netflow/filters'
import type { NfWindow } from '@/lib/netflow/window'
import {
  changeText,
  daySpeedText,
  daysText,
  interfaceLinks,
  DIRECTION_LABELS,
  forecastHeadline,
  growthText,
  overrideShadowNote,
  pReasonText,
  resolutionAction,
  samplesText,
  snmpSentence,
  SOURCE_LABELS,
  speedChangeText,
  statisticLabel,
  trendModel,
  type CapacityPermissions,
  type CapacityRow,
  type Direction,
  type ForecastResult,
  type TrendData,
  type TrendMode,
} from './model'
import { DIRECTION_COLORS, DirectionBadge, Headroom, StateBadge } from './shared'

const TREND_RANGES = [30, 90, 180, 365] as const

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div title={title}>
      <dt className="text-[11px] text-text2">{label}</dt>
      <dd className="text-sm font-medium tabular-nums text-text">{value}</dd>
    </div>
  )
}

function DirectionPanel({ row, d, statistic }: { row: CapacityRow; d: Direction; statistic: string }) {
  const s = row[d]
  return (
    <section className="rounded-md border border-border p-3" aria-label={DIRECTION_LABELS[d]}>
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-text">
        <span className="inline-block h-2 w-3 rounded-sm" style={{ background: DIRECTION_COLORS[d] }} aria-hidden />
        {DIRECTION_LABELS[d]}
      </h4>
      <dl className="grid grid-cols-3 gap-2">
        <Stat label="Average" value={formatBps(s.avg_bps)} />
        <Stat label={statistic} value={s.p_bps != null ? formatBps(s.p_bps) : '—'} title={pReasonText(s.p_reason) ?? undefined} />
        <Stat label="Peak" value={formatBps(s.max_bps)} />
        <Stat label="Speed" value={s.speed_bps ? formatBps(s.speed_bps) : 'Unknown'} title={SOURCE_LABELS[d === 'in' ? row.speed.source_in : row.speed.source_out]} />
        <Stat label={`Util ${s.p_bps != null ? statistic : 'peak'}`} value={formatPercent(s.p_bps != null ? s.utilization_p_pct : s.utilization_max_pct)} />
        <Stat label="Headroom" value={s.headroom_bps == null ? 'Unknown' : s.headroom_bps < 0 ? `Over ${formatBps(-s.headroom_bps)}` : formatBps(s.headroom_bps)} />
      </dl>
    </section>
  )
}

function ForecastCard({ fc, d, levels }: { fc: ForecastResult; d: Direction; levels: { warn: number; crit: number } }) {
  const ok = fc.status === 'ok'
  return (
    <section className="rounded-md border border-border p-3" aria-label={`${DIRECTION_LABELS[d]} forecast`}>
      <h4 className="flex items-center gap-1.5 text-xs font-semibold text-text">
        <span className="inline-block h-2 w-3 rounded-sm" style={{ background: DIRECTION_COLORS[d] }} aria-hidden />
        {DIRECTION_LABELS[d]}
      </h4>
      <p className="mt-1 text-base font-semibold text-text">{forecastHeadline(fc)}</p>
      {fc.reason && <p className="text-[11px] text-text2">{fc.reason}</p>}
      {fc.slope_bps_per_day != null && (
        <dl className="mt-2 grid grid-cols-3 gap-2">
          <Stat label={`To ${formatPercent(levels.warn)}`} value={ok ? daysText(fc.days_to_warn) : '—'} />
          <Stat label={`To ${formatPercent(levels.crit)}`} value={ok ? daysText(fc.days_to_crit) : '—'} />
          <Stat label="To 100%" value={ok ? daysText(fc.days_to_full) : '—'} />
          <Stat label="Growth" value={growthText(fc)} />
          <Stat label="Fit now" value={formatBps(fc.fitted_now_bps)} />
          <Stat
            label="Confidence"
            value={fc.confidence ? `${fc.confidence} (R² ${fc.r2?.toFixed(2)})` : '—'}
            title={`Least-squares line through ${fc.days_used} daily values; R² is how well a straight line explains them`}
          />
        </dl>
      )}
    </section>
  )
}

function TrendChart({ data, statistic, levels, mode }: { data: TrendData; statistic: string; levels: { warn: number; crit: number }; mode: TrendMode }) {
  const m = trendModel(data, levels, mode)
  const pct = mode === 'pct'
  const fmt = (v: number) => (pct ? formatPercent(v) : formatBps(v))
  const lineLabel = (d: Direction) => `${d === 'in' ? 'In' : 'Out'} ${statistic}${pct ? ' utilization' : ''}`
  return (
    <div
      className="h-64 w-full"
      role="img"
      aria-label={`Daily ${statistic} in and out${pct ? ' as a share of each direction\'s speed' : ''}${!pct && !m.symmetric ? `, with ${m.speedLabels.speedIn} and ${m.speedLabels.speedOut}` : ''}; a table view is available below`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={m.points} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.5} vertical={false} />
          <XAxis dataKey="day" tick={{ fontSize: 11, fill: 'rgb(var(--text2))' }} tickLine={false} axisLine={false} minTickGap={24}
            tickFormatter={(v: string) => v.slice(5)} />
          <YAxis tick={{ fontSize: 11, fill: 'rgb(var(--text2))' }} tickLine={false} axisLine={false} width={48}
            tickFormatter={(v: number) => (pct ? `${v}%` : formatBpsAxis(v))} domain={[0, Math.ceil(m.yMax * 1.08) || 1]}
            label={{ value: pct ? '% of speed' : 'bit/s', angle: -90, position: 'insideLeft', fontSize: 11, fill: 'rgb(var(--text2))' }} />
          <Tooltip
            contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 6, fontSize: 12, color: 'rgb(var(--text))' }}
            formatter={(v, name) => [typeof v === 'number' ? fmt(v) : pct ? 'unknown' : 'no percentile', String(name)]}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {pct && <ReferenceLine y={100} stroke="rgb(var(--text2))" strokeDasharray="6 4" label={{ value: 'Speed', position: 'insideTopRight', fontSize: 11, fill: 'rgb(var(--text2))' }} />}
          {pct && <ReferenceLine y={levels.crit} stroke="rgb(var(--danger))" strokeOpacity={0.6} strokeDasharray="2 4" />}
          {pct && <ReferenceLine y={levels.warn} stroke="rgb(var(--warning))" strokeOpacity={0.6} strokeDasharray="2 4" />}
          {!pct && m.anySpeed && m.symmetric && (
            <>
              <Line type="stepAfter" dataKey="speed" name={m.speedLabels.speed} stroke="rgb(var(--text2))" strokeWidth={1.5} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
              <Line type="stepAfter" dataKey="crit" name={`Critical ${formatPercent(levels.crit)}`} stroke="rgb(var(--danger))" strokeOpacity={0.6} strokeWidth={1} strokeDasharray="2 4" dot={false} isAnimationActive={false} legendType="none" />
              <Line type="stepAfter" dataKey="warn" name={`Warning ${formatPercent(levels.warn)}`} stroke="rgb(var(--warning))" strokeOpacity={0.6} strokeWidth={1} strokeDasharray="2 4" dot={false} isAnimationActive={false} legendType="none" />
            </>
          )}
          {!pct && m.anySpeed && !m.symmetric && (
            <>
              <Line type="stepAfter" dataKey="speedIn" name={m.speedLabels.speedIn} stroke={DIRECTION_COLORS.in} strokeOpacity={0.7} strokeWidth={1.5} strokeDasharray="8 4" dot={false} isAnimationActive={false} />
              <Line type="stepAfter" dataKey="speedOut" name={m.speedLabels.speedOut} stroke={DIRECTION_COLORS.out} strokeOpacity={0.7} strokeWidth={1.5} strokeDasharray="8 4" dot={false} isAnimationActive={false} />
            </>
          )}
          <Line type="linear" dataKey="in" name={lineLabel('in')} stroke={DIRECTION_COLORS.in} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
          <Line type="linear" dataKey="out" name={lineLabel('out')} stroke={DIRECTION_COLORS.out} strokeWidth={2} strokeDasharray="5 3" dot={false} connectNulls={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/**
 * One interface: this period's per-direction figures, the daily trend over up to a year and the
 * runout forecast (CR-09, FE-05), with the setup actions its resolution needs.
 */
export function InterfaceDialog({
  row,
  open,
  onOpenChange,
  window: w,
  filter,
  statistic,
  levels,
  perms,
  compare,
  onSetSpeed,
  onLinkExporter,
}: {
  row: CapacityRow | null
  open: boolean
  onOpenChange: (v: boolean) => void
  window: NfWindow
  /** The page filter: drill-down links carry it whole (FE-02), narrowed to this interface. */
  filter: FlowFilter
  statistic: string
  levels: { warn: number; crit: number }
  perms: CapacityPermissions
  compare: boolean
  onSetSpeed: (row: CapacityRow) => void
  onLinkExporter: (row: CapacityRow) => void
}) {
  const [days, setDays] = useState<number>(90)
  const [showTable, setShowTable] = useState(false)
  const [mode, setMode] = useState<TrendMode>('bps')
  const params = row ? nfParams(null, null, { exporter: row.exporter, ifindex: row.ifindex, days, tz: w.tz }) : null
  const trend = useNfQuery<TrendData>('/netflow/capacity/trend', params, { enabled: open && !!row, refetchInterval: false, keepPrevious: false })
  if (!row) return null
  const h = row.headline
  const act = resolutionAction(row, perms)
  const links = interfaceLinks(row, filter, w)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <LineChartIcon className="h-4 w-4 text-primary" aria-hidden />
            {row.display_name}
            <StateBadge state={h.state} />
          </DialogTitle>
          <DialogDescription>
            {row.device_hostname ?? 'No device'} · exporter {ipDisplay(row.exporter)} · ifIndex {row.ifindex}
            {row.if_alias ? ` · ${row.if_alias}` : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap gap-2">
          {(perms.manage || perms.device_speed) && (
            <Button size="sm" variant="outline" onClick={() => onSetSpeed(row)}>
              <Gauge aria-hidden />
              Set speed or name
            </Button>
          )}
          {act.kind === 'link_exporter' && (
            <Button size="sm" variant="outline" onClick={() => onLinkExporter(row)} disabled={act.disabled} title={act.hint}>
              <Cable aria-hidden />
              Link exporter
            </Button>
          )}
          {row.device_id && (
            <Button size="sm" variant="outline" asChild>
              <Link to={row.resolution === 'interface_not_found' ? `/devices/${row.device_id}/interfaces` : `/devices/${row.device_id}?tab=inventory`}>
                <ExternalLink aria-hidden />
                {row.resolution === 'interface_not_found' ? 'Device interfaces' : 'Open device'}
              </Link>
            </Button>
          )}
          <Button size="sm" variant="outline" asChild>
            <Link to={links.overview}>
              <BarChart3 aria-hidden />
              Traffic on this interface
            </Link>
          </Button>
          <Button size="sm" variant="outline" asChild>
            <Link to={links.forensics}>
              <Search aria-hidden />
              Flows on this interface
            </Link>
          </Button>
        </div>
        {act.kind !== 'none' && <p className="rounded-md border border-warning/35 bg-warning/10 p-2 text-xs text-text">{act.hint}</p>}

        <section aria-label="This period" className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-semibold text-text">This period</h3>
            <span className="text-[11px] text-text2">{samplesText(row)}</span>
          </div>
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-surface2/40 p-3">
            <div>
              <div className="text-[11px] text-text2">Utilization ({statisticLabel(h.statistic)}, higher direction)</div>
              <div className="flex items-center gap-2">
                <span className="text-2xl font-semibold tabular-nums text-text">{h.utilization_pct != null ? formatPercent(h.utilization_pct) : 'Unknown'}</span>
                <DirectionBadge direction={h.direction} />
              </div>
            </div>
            <div className="min-w-[12rem] flex-1"><Headroom headline={h} /></div>
            {compare && (
              <div>
                <div className="text-[11px] text-text2">vs previous period</div>
                <div className="text-sm font-medium tabular-nums text-text">{changeText(row.previous?.change_pct)}</div>
              </div>
            )}
          </div>
          {h.note && <p className="text-xs text-danger-fg">{h.note}</p>}
          {overrideShadowNote(row.speed) && (
            <p className="rounded-md border border-warning/35 bg-warning/10 p-2 text-xs text-text">{overrideShadowNote(row.speed)}.</p>
          )}
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            <DirectionPanel row={row} d="in" statistic={statistic} />
            <DirectionPanel row={row} d="out" statistic={statistic} />
          </div>
          <p className="text-[11px] text-text2">
            Combined throughput (in + out, not a utilization): {statistic} {formatBps(row.combined.p_bps)}, peak {formatBps(row.combined.max_bps)}.
            {snmpSentence(row.snmp) && ` ${snmpSentence(row.snmp)}`}
            {row.counters?.status === 'ok' && ` sFlow counters: in ${formatBps(row.counters.in_p_bps ?? row.counters.in_max_bps)}, out ${formatBps(row.counters.out_p_bps ?? row.counters.out_max_bps)}.`}
          </p>
        </section>

        <section aria-label="Trend and forecast" className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-text">Daily trend and forecast</h3>
            <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Trend range">
              <div className="mr-1 flex items-center gap-1" role="group" aria-label="Trend scale">
                <Button size="sm" variant={mode === 'bps' ? 'default' : 'outline'} aria-pressed={mode === 'bps'} onClick={() => setMode('bps')}>bit/s</Button>
                <Button size="sm" variant={mode === 'pct' ? 'default' : 'outline'} aria-pressed={mode === 'pct'} onClick={() => setMode('pct')}
                  title="Each direction as a share of its own speed on that day">% of speed</Button>
              </div>
              {TREND_RANGES.map((r) => (
                <Button key={r} size="sm" variant={days === r ? 'default' : 'outline'} aria-pressed={days === r} onClick={() => setDays(r)}>
                  {r} d
                </Button>
              ))}
              <Button size="sm" variant="ghost" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
                <Table2 aria-hidden />
                {showTable ? 'Hide table' : 'Table'}
              </Button>
            </div>
          </div>
          <QueryPanel
            query={trend}
            label="the interface trend"
            skeleton="chart"
            isEmpty={(r) => r.data.days.every((d) => d.samples === 0)}
            empty="No flow samples for this interface in this range"
          >
            {(r) => {
              const t = r.data
              return (
                <div className="space-y-3">
                  <DataQualityBar meta={r.meta} coverage={null} />
                  <TrendChart data={t} statistic={statistic} levels={levels} mode={mode} />
                  <p className="text-[11px] text-text2">
                    Complete local days ({t.window.tz}) only; days without enough samples for a percentile are gaps.{' '}
                    {mode === 'pct'
                      ? 'Each direction is divided by its own speed in effect that day; dashed: 100%, dotted: warning and critical levels.'
                      : trendModel(t, levels, 'bps').symmetric
                        ? 'Dashed grey: speed in effect each day; dotted: warning and critical levels.'
                        : 'Dashed in the direction colours: each direction\'s own speed in effect each day; switch to % of speed for the warning and critical levels.'}{' '}
                    {t.method.forecast}.
                  </p>
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                    <ForecastCard fc={t.forecast.in} d="in" levels={levels} />
                    <ForecastCard fc={t.forecast.out} d="out" levels={levels} />
                  </div>
                  {t.speed_history.length > 0 && (
                    <div className="text-xs text-text">
                      <h4 className="mb-1 font-semibold">Speed changes (audit log)</h4>
                      <ul className="space-y-0.5 text-text2">
                        {t.speed_history.slice(0, 12).map((s, i) => (
                          <li key={i}>
                            {formatDateTime(s.at, t.window.tz, { zone: 'none', seconds: false })} · {speedChangeText(s)}
                            {s.actor ? ` by ${s.actor}` : ''}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {showTable && (
                    <div className="max-h-72 overflow-auto rounded-md border border-border">
                      <Table>
                        <THead>
                          <Tr>
                            <Th>Day</Th>
                            <Th className="text-right">In {statistic}</Th>
                            <Th className="text-right">Out {statistic}</Th>
                            <Th className="text-right">In peak</Th>
                            <Th className="text-right">Out peak</Th>
                            <Th className="text-right" title="Speed in effect that day (in / out when they differ)">Speed</Th>
                            <Th className="text-right">Samples</Th>
                          </Tr>
                        </THead>
                        <TBody>
                          {[...t.days].reverse().map((d) => (
                            <Tr key={d.day}>
                              <Td className="whitespace-nowrap">{d.day}{d.complete ? '' : ' (partial)'}</Td>
                              <Td className="text-right tabular-nums">{formatBps(d.in.p_bps)}</Td>
                              <Td className="text-right tabular-nums">{formatBps(d.out.p_bps)}</Td>
                              <Td className="text-right tabular-nums">{formatBps(d.in.max_bps)}</Td>
                              <Td className="text-right tabular-nums">{formatBps(d.out.max_bps)}</Td>
                              <Td className="whitespace-nowrap text-right tabular-nums">{daySpeedText(d.speed)}{d.speed?.changed ? ' *' : ''}</Td>
                              <Td className={cn('text-right tabular-nums', d.p_reason && 'text-text2')} title={pReasonText(d.p_reason) ?? undefined}>
                                {formatCount(d.samples)}/{formatCount(d.expected_slots)}
                              </Td>
                            </Tr>
                          ))}
                        </TBody>
                      </Table>
                    </div>
                  )}
                </div>
              )
            }}
          </QueryPanel>
        </section>
      </DialogContent>
    </Dialog>
  )
}
