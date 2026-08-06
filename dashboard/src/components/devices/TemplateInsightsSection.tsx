import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { ChevronDown, ChevronRight, LayoutTemplate, LineChart as LineChartIcon } from 'lucide-react'
import { api } from '@/lib/api'
import {
  axisRightPad, cn, formatBps, formatBytes, relativeTime,
  timeAxisTickFormatter, timeTicks, timeTooltipLabelFormatter,
} from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'

/* Vendor-template insights for one device: the groups declared by the
 * attached monitoring template, rendered from /devices/{id}/template-insights.
 * Scalar groups become KPI tiles (click = history chart); table groups become
 * status tables (VPN tunnels, HA members, APs, FortiSwitches, pools…). */

type InsightMetric = {
  key: string; name: string; type: string; unit?: string | null
  thresholds?: { warn?: number; crit?: number; op?: string } | null
  value: number | null; text: string; status: string
  series_key: string; has_data: boolean
}
type InsightCell = { value: number | null; text: string; status: string; series_key: string }
type InsightRow = { instance: string; label: string; cells: Record<string, InsightCell> }
type InsightColumn = { key: string; name: string; unit?: string | null; type: string }
type InsightGroup = {
  key: string; name: string; kind: 'scalar' | 'table'
  description?: string | null; status: string
  metrics?: InsightMetric[]
  columns?: InsightColumn[]
  rows?: InsightRow[]
}
type Insights = {
  template: { id: string; name: string; vendor?: string | null } | null
  updated_at: string | null
  groups: InsightGroup[]
}

const statusText: Record<string, string> = {
  ok: 'text-success', warn: 'text-warning', crit: 'text-danger',
  info: 'text-info', none: 'text-text',
}
const statusDot: Record<string, string> = {
  ok: 'bg-success', warn: 'bg-warning', crit: 'bg-danger',
  info: 'bg-info', none: 'bg-muted/40',
}

function formatValue(v: number | null, unit?: string | null): string {
  if (v == null) return '—'
  const u = (unit || '').toLowerCase()
  if (u === 'bps') return formatBps(v)
  if (u === 'bytes') return formatBytes(v)
  const num = Math.abs(v) >= 1000
    ? Math.round(v).toLocaleString()
    : (Number.isInteger(v) ? v.toString() : v.toFixed(1))
  if (u === '%') return `${num}%`
  return unit ? `${num} ${unit}` : num
}

export function TemplateInsightsSection({ deviceId, rangeHours }: {
  deviceId: string; rangeHours: number
}) {
  const [chart, setChart] = useState<{ seriesKey: string; title: string; unit?: string | null } | null>(null)

  const { data } = useQuery<Insights>({
    queryKey: ['device', deviceId, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/template-insights`)).data,
    refetchInterval: 30_000,
  })

  if (!data?.template) return null
  const groups = (data.groups || []).filter((g) =>
    g.kind === 'table' ? (g.rows?.length || 0) > 0 : (g.metrics || []).some((m) => m.has_data),
  )

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <LayoutTemplate className="h-4 w-4 text-primary" />
        <h2 className="text-sm font-semibold">Vendor Insights</h2>
        <Badge variant="info">{data.template.name}</Badge>
        {data.updated_at ? (
          <span className="text-[11px] text-muted">updated {relativeTime(data.updated_at)}</span>
        ) : (
          <span className="text-[11px] text-muted">waiting for first template poll…</span>
        )}
      </div>

      {groups.length === 0 ? (
        <Card><CardContent className="p-6 text-center text-xs text-muted">
          Template attached — collecting vendor metrics… data appears within one or two SNMP polls.
        </CardContent></Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {groups.map((g) => (
            g.kind === 'table'
              ? <TableGroupCard key={g.key} group={g}
                  onChart={(cell, row, col) => setChart({ seriesKey: cell.series_key, title: `${col.name} — ${row.label}`, unit: col.unit })} />
              : <ScalarGroupCard key={g.key} group={g}
                  onChart={(m) => setChart({ seriesKey: m.series_key, title: m.name, unit: m.unit })} />
          ))}
        </div>
      )}

      {chart && (
        <MetricChartDialog deviceId={deviceId} rangeHours={rangeHours}
          seriesKey={chart.seriesKey} title={chart.title} unit={chart.unit}
          onOpenChange={(o) => !o && setChart(null)} />
      )}
    </div>
  )
}

function GroupStatusBadge({ status }: { status: string }) {
  if (status === 'crit') return <Badge variant="danger">Attention</Badge>
  if (status === 'warn') return <Badge variant="warning">Warning</Badge>
  if (status === 'ok') return <Badge variant="success">Healthy</Badge>
  return null
}

/* ── Scalar group: KPI tile grid ─────────────────────────────────── */

function ScalarGroupCard({ group: g, onChart }: {
  group: InsightGroup; onChart: (m: InsightMetric) => void
}) {
  const metrics = (g.metrics || []).filter((m) => m.has_data)
  const numeric = metrics.filter((m) => m.type !== 'string')
  const strings = metrics.filter((m) => m.type === 'string' && m.text)
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <h3 className="text-sm font-semibold">{g.name}</h3>
          <GroupStatusBadge status={g.status} />
        </div>
        {numeric.length > 0 && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {numeric.map((m) => (
              <button key={m.key} type="button" onClick={() => onChart(m)}
                title="Show history"
                className="group/tile rounded-lg border border-border bg-surface2 px-3 py-2.5 text-left transition-colors hover:border-primary/50">
                <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wide text-muted">
                  <span className={cn('h-1.5 w-1.5 rounded-full', statusDot[m.status] || statusDot.none)} />
                  <span className="truncate">{m.name}</span>
                  <LineChartIcon className="ml-auto h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover/tile:opacity-60" />
                </div>
                <div className={cn('mt-1 truncate text-base font-bold tabular-nums', statusText[m.status] || statusText.none)}>
                  {m.type === 'enum' && m.text ? m.text : formatValue(m.value, m.unit)}
                </div>
              </button>
            ))}
          </div>
        )}
        {strings.length > 0 && (
          <div className={cn('grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2', numeric.length > 0 && 'mt-3 border-t border-border pt-3')}>
            {strings.map((m) => (
              <div key={m.key} className="flex items-baseline justify-between gap-2 text-xs">
                <span className="shrink-0 text-muted">{m.name}</span>
                <span className="truncate font-medium" title={m.text}>{m.text}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Table group: rows with status cells ─────────────────────────── */

function TableGroupCard({ group: g, onChart }: {
  group: InsightGroup
  onChart: (cell: InsightCell, row: InsightRow, col: InsightColumn) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const rows = g.rows || []
  const cols = (g.columns || []).filter((c) =>
    rows.some((r) => r.cells[c.key] !== undefined))
  const shown = expanded ? rows : rows.slice(0, 8)
  return (
    <Card className={cn(rows.length > 6 && 'xl:col-span-2')}>
      <CardContent className="p-4">
        <div className="mb-2 flex items-center gap-2">
          <h3 className="text-sm font-semibold">{g.name}</h3>
          <span className="text-[11px] text-muted">{rows.length} row{rows.length === 1 ? '' : 's'}</span>
          <GroupStatusBadge status={g.status} />
        </div>
        <div className="overflow-x-auto">
          <Table>
            <THead>
              <Tr>
                <Th className="whitespace-nowrap">Name</Th>
                {cols.map((c) => <Th key={c.key} className="whitespace-nowrap">{c.name}</Th>)}
              </Tr>
            </THead>
            <TBody>
              {shown.map((r) => (
                <Tr key={r.instance}>
                  <Td className="max-w-[180px] truncate text-xs font-medium" title={r.label}>{r.label}</Td>
                  {cols.map((c) => {
                    const cell = r.cells[c.key]
                    if (!cell) return <Td key={c.key} className="text-xs text-muted">—</Td>
                    const isEnum = c.type === 'enum'
                    const isNum = c.type !== 'string' && cell.value != null
                    return (
                      <Td key={c.key} className="whitespace-nowrap text-xs">
                        {isEnum ? (
                          <span className={cn('inline-flex items-center gap-1.5 font-medium', statusText[cell.status] || '')}>
                            <span className={cn('h-1.5 w-1.5 rounded-full', statusDot[cell.status] || statusDot.none)} />
                            {cell.text || cell.value}
                          </span>
                        ) : isNum ? (
                          <button type="button" title="Show history" onClick={() => onChart(cell, r, c)}
                            className={cn('cursor-pointer tabular-nums hover:underline', statusText[cell.status] || '')}>
                            {formatValue(cell.value, c.unit)}
                          </button>
                        ) : (
                          <span className="max-w-[160px] truncate text-muted" title={cell.text}>{cell.text || '—'}</span>
                        )}
                      </Td>
                    )
                  })}
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
        {rows.length > shown.length && !expanded && (
          <button type="button" onClick={() => setExpanded(true)}
            className="mt-2 flex items-center gap-1 text-[11px] text-primary hover:underline">
            <ChevronDown className="h-3 w-3" /> Show all {rows.length}
          </button>
        )}
        {expanded && rows.length > 8 && (
          <button type="button" onClick={() => setExpanded(false)}
            className="mt-2 flex items-center gap-1 text-[11px] text-muted hover:underline">
            <ChevronRight className="h-3 w-3" /> Collapse
          </button>
        )}
      </CardContent>
    </Card>
  )
}

/* ── History chart dialog (reads /snmp-metrics, recharts) ────────── */

function MetricChartDialog({ deviceId, seriesKey, title, unit, rangeHours, onOpenChange }: {
  deviceId: string; seriesKey: string; title: string; unit?: string | null
  rangeHours: number; onOpenChange: (o: boolean) => void
}) {
  const hours = Math.max(rangeHours, 1)
  const { data, isLoading } = useQuery<Record<string, { unit: string; points: { ts_ms: number; value: number }[] }>>({
    queryKey: ['device', deviceId, 'snmp-metrics', hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-metrics?hours=${hours}`)).data,
  })

  const points = useMemo(
    () => (data?.[seriesKey]?.points || []).map((p) => ({ ts: p.ts_ms, value: p.value })),
    [data, seriesKey],
  )
  const toTs = Date.now()
  const fromTs = toTs - hours * 3600_000
  const ticks = useMemo(() => timeTicks(fromTs, toTs, hours), [fromTs, toTs, hours])
  const tickFormatter = useMemo(() => timeAxisTickFormatter(hours), [hours])
  const u = (unit || '').toLowerCase()
  const fmt = (v: number) =>
    u === 'bps' ? formatBps(v) : u === 'bytes' ? formatBytes(v) : formatValue(v, unit)

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-base">{title}</DialogTitle>
          <DialogDescription>Last {hours >= 24 ? `${Math.round(hours / 24)}d` : `${hours}h`} · template metric history</DialogDescription>
        </DialogHeader>
        <div className="h-64">
          {isLoading ? (
            <div className="flex h-full items-center justify-center text-xs text-muted">Loading…</div>
          ) : points.length === 0 ? (
            <div className="flex h-full items-center justify-center text-xs text-muted">
              No history yet — samples land in ClickHouse on each SNMP poll.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={points} margin={{ top: 6, right: axisRightPad(hours), bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="tplFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.28} />
                    <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                <XAxis dataKey="ts" type="number" scale="time" domain={[fromTs, toTs]} ticks={ticks}
                  interval={0} tickFormatter={tickFormatter}
                  tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <YAxis width={52} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                  axisLine={false} tickLine={false} tickFormatter={(v: number) => fmt(v)} />
                <Tooltip
                  labelFormatter={timeTooltipLabelFormatter}
                  formatter={(v: any) => [fmt(Number(v)), title]}
                  contentStyle={{
                    background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))',
                    borderRadius: 8, fontSize: 12, color: 'rgb(var(--text))',
                  }} />
                <Area type="monotone" dataKey="value" stroke="rgb(var(--primary))" strokeWidth={1.8}
                  fill="url(#tplFill)" connectNulls />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
