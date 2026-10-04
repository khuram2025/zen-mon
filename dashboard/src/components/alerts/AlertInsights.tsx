/**
 * Alert Center insights (F09, F12): opened vs closed trend, open-incident
 * aging, top rules / entities and backlog-health diagnostics.
 *
 * Every panel states its period, timezone basis and that recovery / dependency
 * / hold-cancelled notices are excluded. Nothing is computed from the loaded
 * table page — all figures come from /alerts/summary and /alerts/trend.
 */
import { useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useVizColors } from '@/components/availability/viz'
import { ENTITY_ICON, ENTITY_KIND_LABEL, focusRing } from './AlertBits'
import { fmtAlertDate, parseTs, timezoneName } from './format'
import type { AlertSummary, AlertTrend, EntityKind } from './types'

const TREND_RANGES = [
  { hours: 24, label: '24 h' },
  { hours: 168, label: '7 days' },
  { hours: 720, label: '30 days' },
  { hours: 2160, label: '90 days' },
]

export function AlertInsights({
  filterQs, summary, summaryError, onFilter, scopeLabel,
}: {
  /** Entity/severity/search filters (URLSearchParams string) shared with the list. */
  filterQs: string
  summary: AlertSummary | undefined
  summaryError: boolean
  /** Patch URL filters (drill-down). */
  onFilter: (patch: Record<string, string | null>) => void
  scopeLabel: string
}) {
  const [hours, setHours] = useState(168)
  const qs = useMemo(() => {
    const p = new URLSearchParams(filterQs)
    p.set('hours', String(hours))
    return p.toString()
  }, [filterQs, hours])

  const trend = useQuery<AlertTrend>({
    queryKey: ['alerts', 'trend', qs],
    queryFn: async () => (await api.get(`/alerts/trend?${qs}`)).data,
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  })
  const rangeLabel = TREND_RANGES.find((r) => r.hours === hours)?.label ?? `${hours} h`

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          Scope: <span className="text-text2">{scopeLabel}</span> · Times in {timezoneName()}, windows on the appliance clock ·
          Incidents only (recovery, dependency and hold-cancelled notices excluded)
        </p>
        <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Trend period">
          {TREND_RANGES.map((r) => (
            <button
              key={r.hours}
              aria-pressed={hours === r.hours}
              onClick={() => setHours(r.hours)}
              className={cn('rounded px-2.5 py-1 text-sm', focusRing, hours === r.hours ? 'bg-surface font-medium text-text shadow-sm' : 'text-muted hover:text-text')}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <section className="min-w-0 rounded-lg border border-border bg-surface p-4" aria-labelledby="trend-h">
          <h3 id="trend-h" className="text-base font-semibold">Opened vs closed</h3>
          <p className="text-sm text-muted">
            Last {rangeLabel}{trend.data ? `, ${trend.data.bucket === 'hour' ? 'hourly' : 'daily'} buckets` : ''}.
            Closed is split into genuine recoveries and other closures (manual, suppressed, superseded).
          </p>
          {trend.isLoading ? (
            <div className="flex h-60 items-center justify-center text-muted"><Loader2 className="h-5 w-5 animate-spin" /></div>
          ) : trend.isError && !trend.data ? (
            <InsightError message={apiErrorMessage(trend.error)} onRetry={() => trend.refetch()} />
          ) : trend.data ? (
            <>
              {trend.isError && <p className="mt-1 text-sm text-warning">Refresh failed — showing the previous result.</p>}
              <TrendChart trend={trend.data} />
            </>
          ) : null}
        </section>

        <section className="min-w-0 rounded-lg border border-border bg-surface p-4" aria-labelledby="aging-h">
          <h3 id="aging-h" className="text-base font-semibold">Open incident aging</h3>
          <p className="text-sm text-muted">Open now (active + acknowledged + suppressed + pending), by time since triggered.</p>
          {summary ? (
            <AgingBars aging={summary.open.aging} dim={summaryError} onDrill={() => onFilter({ view: 'open', age: 'gt7d', page: null })} />
          ) : summaryError ? (
            <p className="mt-3 text-sm text-muted">Unavailable.</p>
          ) : (
            <div className="flex h-40 items-center justify-center text-muted"><Loader2 className="h-5 w-5 animate-spin" /></div>
          )}
        </section>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <section className="min-w-0 rounded-lg border border-border bg-surface p-4" aria-labelledby="toprules-h">
          <h3 id="toprules-h" className="text-base font-semibold">Top rules</h3>
          <p className="text-sm text-muted">Incidents opened, last {rangeLabel}. Click to filter.</p>
          <TopList
            loading={trend.isLoading}
            items={(trend.data?.top_rules ?? []).map((r) => ({
              key: r.rule_id, label: r.name, count: r.count,
              onClick: () => onFilter({ rule_id: r.rule_id, page: null }),
            }))}
          />
        </section>
        <section className="min-w-0 rounded-lg border border-border bg-surface p-4" aria-labelledby="topent-h">
          <h3 id="topent-h" className="text-base font-semibold">Top entities</h3>
          <p className="text-sm text-muted">Incidents opened, last {rangeLabel}. Click to filter.</p>
          <TopList
            loading={trend.isLoading}
            items={(trend.data?.top_entities ?? []).map((e) => ({
              key: `${e.kind}:${e.id}`,
              label: e.name,
              kind: e.kind,
              count: e.count,
              onClick: () => onFilter(entityFilter(e.kind, e.id)),
            }))}
          />
        </section>
        <section className="min-w-0 rounded-lg border border-border bg-surface p-4" aria-labelledby="health-h">
          <h3 id="health-h" className="text-base font-semibold">Backlog health</h3>
          <p className="text-sm text-muted">Open incidents that need a governance decision, not automatic clearing.</p>
          {summary ? (
            <ul className={cn('mt-3 space-y-2', summaryError && 'opacity-60')}>
              <HealthRow
                label="Rule disabled"
                hint="The rule that raised them is switched off; they will never auto-recover."
                value={summary.diagnostics.open_rule_disabled}
                onClick={() => onFilter({ view: 'open', rule_state: 'disabled', page: null })}
              />
              <HealthRow
                label="Rule deleted"
                hint="No rule exists any more for these incidents."
                value={summary.diagnostics.open_rule_missing}
                onClick={() => onFilter({ view: 'open', rule_state: 'missing', page: null })}
              />
              <HealthRow
                label="Open longer than 7 days"
                hint="Investigate before closing — they may be genuine long outages."
                value={summary.diagnostics.open_older_7d}
                onClick={() => onFilter({ view: 'open', age: 'gt7d', page: null })}
              />
              <HealthRow
                label="Notification failed"
                hint="A channel rejected or could not be reached for these open incidents; nobody may have been told."
                value={summary.diagnostics.open_delivery_failed ?? 0}
                onClick={() => onFilter({ view: 'open', delivery: 'failed', page: null })}
              />
            </ul>
          ) : (
            <p className="mt-3 text-sm text-muted">{summaryError ? 'Unavailable.' : 'Loading…'}</p>
          )}
        </section>
      </div>
    </div>
  )
}

export function entityFilter(kind: EntityKind, id: string | null): Record<string, string | null> {
  const base: Record<string, string | null> = {
    device_id: null, server_id: null, service_check_id: null, sensor_id: null, entity_kind: null, page: null,
  }
  const key = kind === 'device' ? 'device_id' : kind === 'server' ? 'server_id' : kind === 'service_check' ? 'service_check_id' : kind === 'sensor' ? 'sensor_id' : null
  if (key && id) base[key] = id
  else base.entity_kind = kind
  return base
}

function InsightError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="mt-3 flex h-48 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border text-sm text-muted">
      <span>Trend unavailable: {message}</span>
      <button onClick={onRetry} className={cn('inline-flex items-center gap-1 rounded px-2 py-1 text-primary hover:underline', focusRing)}>
        <RefreshCw className="h-3.5 w-3.5" /> Retry
      </button>
    </div>
  )
}

function TrendChart({ trend }: { trend: AlertTrend }) {
  const colors = useVizColors()
  const option = useMemo(() => {
    const muted = colors.c('muted')
    const hourly = trend.bucket === 'hour'
    const pts = trend.series.map((p) => ({ ...p, ms: parseTs(p.t) ?? 0 }))
    const label = (ms: number) => {
      const d = new Date(ms)
      return hourly
        ? d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
        : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
    }
    const tip = {
      backgroundColor: colors.c('surface'),
      borderColor: colors.c('border'),
      borderWidth: 1,
      padding: [8, 10],
      textStyle: { color: colors.c('text'), fontSize: 12 },
      extraCssText: 'box-shadow: 0 8px 24px rgba(0,0,0,.25); border-radius: 8px;',
    }
    return {
      animationDuration: 500,
      grid: { left: 40, right: 12, top: 30, bottom: 40 },
      legend: {
        top: 0, left: 0, itemWidth: 12, itemHeight: 8,
        textStyle: { color: colors.c('text2'), fontSize: 12 },
        data: ['Opened', 'Recovered', 'Closed (other)'],
      },
      xAxis: {
        type: 'category',
        data: pts.map((p) => label(p.ms)),
        axisLine: { lineStyle: { color: colors.c('border') } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11, hideOverlap: true },
      },
      yAxis: {
        type: 'value', minInterval: 1,
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { lineStyle: { color: colors.c('border', 0.8), type: 'dashed' } },
      },
      tooltip: {
        ...tip,
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          const p = pts[arr[0]?.dataIndex ?? 0]
          if (!p) return ''
          const row = (name: string, v: number, c: string) =>
            `<div style="display:flex;justify-content:space-between;gap:16px"><span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${c};margin-right:6px"></span>${name}</span><b>${v}</b></div>`
          return `<div style="font-weight:600;margin-bottom:4px">${fmtAlertDate(p.ms)}</div>`
            + row('Opened', p.opened, colors.c('warning'))
            + row('Recovered', p.recovered, colors.c('success'))
            + row('Closed (other)', p.closed_other, colors.c('muted'))
        },
      },
      series: [
        {
          name: 'Opened', type: 'bar', barMaxWidth: 14, barGap: '15%',
          data: pts.map((p) => p.opened),
          itemStyle: { color: colors.c('warning'), borderRadius: [3, 3, 0, 0] },
        },
        {
          name: 'Recovered', type: 'bar', stack: 'closed', barMaxWidth: 14,
          data: pts.map((p) => p.recovered),
          itemStyle: { color: colors.c('success') },
        },
        {
          name: 'Closed (other)', type: 'bar', stack: 'closed', barMaxWidth: 14,
          data: pts.map((p) => p.closed_other),
          itemStyle: { color: colors.c('muted', 0.7), borderRadius: [3, 3, 0, 0] },
        },
      ],
    }
  }, [trend, colors])
  const total = trend.series.reduce((a, p) => ({ o: a.o + p.opened, r: a.r + p.recovered, c: a.c + p.closed_other }), { o: 0, r: 0, c: 0 })
  return (
    <div>
      <div
        role="img"
        aria-label={`Opened ${total.o}, recovered ${total.r}, closed other ${total.c} over the period`}
      >
        <ReactECharts option={option} notMerge style={{ height: 260, width: '100%' }} />
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted">
        <span>Opened <b className="text-text">{total.o.toLocaleString()}</b></span>
        <span>Recovered <b className="text-text">{total.r.toLocaleString()}</b></span>
        <span>Closed (other) <b className="text-text">{total.c.toLocaleString()}</b></span>
      </div>
    </div>
  )
}

function AgingBars({ aging, dim, onDrill }: { aging: AlertSummary['open']['aging']; dim: boolean; onDrill: () => void }) {
  const rows = [
    { key: 'lt_1h', label: '< 1 hour', value: aging.lt_1h, cls: 'bg-primary/60' },
    { key: 'h1_24', label: '1–24 hours', value: aging.h1_24, cls: 'bg-warning/60' },
    { key: 'd1_7', label: '1–7 days', value: aging.d1_7, cls: 'bg-warning' },
    { key: 'gt_7d', label: '> 7 days', value: aging.gt_7d, cls: 'bg-danger' },
  ]
  const max = Math.max(1, ...rows.map((r) => r.value))
  return (
    <ul className={cn('mt-3 space-y-2.5', dim && 'opacity-60')}>
      {rows.map((r) => (
        <li key={r.key} className="grid grid-cols-[88px_1fr_56px] items-center gap-2 text-sm">
          <span className="text-text2">{r.label}</span>
          <span className="h-3 overflow-hidden rounded bg-surface2" aria-hidden>
            <span className={cn('block h-full rounded', r.cls)} style={{ width: `${(r.value / max) * 100}%` }} />
          </span>
          {r.key === 'gt_7d' && r.value > 0 ? (
            <button onClick={onDrill} className={cn('text-right font-semibold tabular-nums text-danger hover:underline', focusRing)} aria-label={`Show ${r.value} incidents open longer than 7 days`}>
              {r.value.toLocaleString()}
            </button>
          ) : (
            <span className="text-right font-semibold tabular-nums">{r.value.toLocaleString()}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

function TopList({ items, loading }: { loading: boolean; items: Array<{ key: string; label: string; count: number; kind?: EntityKind; onClick: () => void }> }) {
  if (loading) return <p className="mt-3 text-sm text-muted">Loading…</p>
  if (!items.length) return <p className="mt-3 text-sm text-muted">No incidents opened in this period.</p>
  const max = Math.max(1, ...items.map((i) => i.count))
  return (
    <ol className="mt-3 space-y-1">
      {items.slice(0, 8).map((i) => {
        const Icon = i.kind ? ENTITY_ICON[i.kind] : null
        return (
          <li key={i.key}>
            <button
              onClick={i.onClick}
              className={cn('group relative flex w-full items-center gap-2 overflow-hidden rounded px-2 py-1.5 text-left text-sm hover:bg-surface2', focusRing)}
              title={i.kind ? `${ENTITY_KIND_LABEL[i.kind]}: ${i.label}` : i.label}
            >
              <span className="absolute inset-y-0 left-0 bg-primary/10" style={{ width: `${(i.count / max) * 100}%` }} aria-hidden />
              {Icon && <Icon className="relative h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
              <span className="relative min-w-0 flex-1 truncate">{i.label}</span>
              <span className="relative font-semibold tabular-nums">{i.count.toLocaleString()}</span>
              <ArrowRight className="relative h-3.5 w-3.5 shrink-0 text-muted opacity-0 group-hover:opacity-100" aria-hidden />
            </button>
          </li>
        )
      })}
    </ol>
  )
}

function HealthRow({ label, hint, value, onClick }: { label: string; hint: string; value: number; onClick: () => void }) {
  const bad = value > 0
  return (
    <li className={cn('flex items-start gap-2 rounded-md border p-2.5', bad ? 'border-warning/40 bg-warning/5' : 'border-border')}>
      <AlertTriangle className={cn('mt-0.5 h-4 w-4 shrink-0', bad ? 'text-warning' : 'text-muted')} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{label}</div>
        <div className="text-xs text-muted">{hint}</div>
      </div>
      {bad ? (
        <button onClick={onClick} className={cn('text-base font-semibold tabular-nums text-warning hover:underline', focusRing)} aria-label={`Show ${value} open incidents: ${label}`}>
          {value.toLocaleString()}
        </button>
      ) : (
        <span className="text-base font-semibold tabular-nums text-muted">0</span>
      )}
    </li>
  )
}
