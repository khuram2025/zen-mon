import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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
import { Switch } from '@/components/ui/Switch'
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
type InsightRow = {
  instance: string; label: string; cells: Record<string, InsightCell>
  // Set on children-capable groups when this row has been materialized as a
  // child device (managed AP / switch promoted to its own device page).
  child_device_id?: string | null
  // Set by collapseRepeatedRows when several identical device rows were folded
  // into this one.
  dupCount?: number
}
type InsightColumn = { key: string; name: string; unit?: string | null; type: string }
type InsightGroup = {
  key: string; name: string; kind: 'scalar' | 'table'
  description?: string | null; status: string
  children_capable?: boolean
  metrics?: InsightMetric[]
  columns?: InsightColumn[]
  rows?: InsightRow[]
}
type Insights = {
  template: { id: string; name: string; vendor?: string | null } | null
  updated_at: string | null
  groups: InsightGroup[]
  // True when the template declares managed-device tables (APs, switches)
  // that can be promoted to child devices of this controller.
  children_capable?: boolean
  promote_managed?: boolean
}

const statusText: Record<string, string> = {
  ok: 'text-success', warn: 'text-warning', crit: 'text-danger',
  info: 'text-info', none: 'text-text',
}
const statusDot: Record<string, string> = {
  ok: 'bg-success', warn: 'bg-warning', crit: 'bg-danger',
  info: 'bg-info', none: 'bg-muted/40',
}

/* Columns worth rendering. Two kinds get dropped:
 *  - columns no row carries a value for;
 *  - columns whose text is the row label repeated on every row. Several vendor
 *    packs point a column at an OID that returns the same string the row is
 *    keyed by (FortiGate's IPsec "Phase 2" name is the tunnel name), which
 *    reads as a duplicated column. */
function visibleColumns(g: InsightGroup): InsightColumn[] {
  const rows = g.rows || []
  return (g.columns || []).filter((c) => {
    if (!rows.some((r) => r.cells[c.key] !== undefined)) return false
    const echoesLabel = rows.every((r) => {
      const cell = r.cells[c.key]
      return cell && cell.value == null && (cell.text || '') === r.label
    })
    return !echoesLabel
  })
}

/* Numbers and enums are read by scanning down a column, so they sit right;
 * free text reads as prose and stays left. */
function alignOf(type: string): string {
  return type === 'string' ? 'text-left' : 'text-right'
}

const SEV_RANK: Record<string, number> = { none: 0, info: 1, ok: 2, warn: 3, crit: 4 }

/* Some vendor tables carry one row per sub-entry of a parent object and repeat
 * the parent's name and totals on each. FortiGate's fgVpn2TunTable is indexed
 * phase1.phase2 and returns the phase-1 name and the phase-1 aggregate byte
 * counters on every phase-2 SA — a tunnel with 19 selectors renders as 19
 * identical rows showing the same traffic. The walk is faithful, but repeating
 * it verbatim reads as a bug and invites summing counters that are already
 * totals.
 *
 * So: fold rows that share a label AND agree on every measured value. Rows
 * whose numbers differ are genuinely distinct and are always left alone.
 * Differing states are rolled up ("18 Up · 1 Down") so a single failed SA
 * stays visible instead of being hidden by the fold. */
function collapseRepeatedRows(rows: InsightRow[], cols: InsightColumn[]): InsightRow[] {
  const byLabel = new Map<string, InsightRow[]>()
  for (const r of rows) {
    const key = r.label || r.instance
    const bucket = byLabel.get(key)
    if (bucket) bucket.push(r)
    else byLabel.set(key, [r])
  }
  if (byLabel.size === rows.length) return rows

  const measured = cols.filter((c) => c.type !== 'enum' && c.type !== 'string')
  const out: InsightRow[] = []
  for (const group of byLabel.values()) {
    if (group.length === 1) { out.push(group[0]); continue }

    const first = group[0]
    const sameNumbers = measured.every((c) => {
      const v = first.cells[c.key]?.value ?? null
      return group.every((r) => (r.cells[c.key]?.value ?? null) === v)
    })
    if (!sameNumbers) { out.push(...group); continue }

    const cells: Record<string, InsightCell> = { ...first.cells }
    for (const c of cols) {
      if (c.type !== 'enum') continue
      const texts = group.map((r) => r.cells[c.key]?.text || '').filter(Boolean)
      const distinct = [...new Set(texts)]
      if (distinct.length <= 1) continue
      const worst = group.reduce((w, r) => {
        const s = r.cells[c.key]?.status || 'none'
        return (SEV_RANK[s] ?? 0) > (SEV_RANK[w] ?? 0) ? s : w
      }, 'none')
      cells[c.key] = {
        value: null,
        text: distinct.map((t) => `${texts.filter((x) => x === t).length} ${t}`).join(' · '),
        status: worst,
        series_key: first.cells[c.key]?.series_key || '',
      }
    }
    out.push({ ...first, cells, dupCount: group.length })
  }
  return out
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
  const queryClient = useQueryClient()

  const { data } = useQuery<Insights>({
    queryKey: ['device', deviceId, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/template-insights`)).data,
    refetchInterval: 30_000,
  })

  const promote = useMutation({
    mutationFn: async (on: boolean) =>
      (await api.put(`/devices/${deviceId}`, { promote_managed: on })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['device', deviceId] })
      queryClient.invalidateQueries({ queryKey: ['devices'] })
    },
  })

  if (!data?.template) return null
  const groups = (data.groups || []).filter((g) =>
    g.kind === 'table' ? (g.rows?.length || 0) > 0 : (g.metrics || []).some((m) => m.has_data),
  )
  const promotedCount = groups
    .filter((g) => g.children_capable)
    .reduce((n, g) => n + (g.rows || []).filter((r) => r.child_device_id).length, 0)

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
        {data.children_capable && (
          <span
            className="ml-auto inline-flex items-center gap-2 text-[11px] text-muted"
            title="Create a child device for every AP/switch this controller manages, so alert rules, maintenance windows, tags and reports apply to each one. A controller outage raises a single root-cause alert; child alerts are suppressed."
          >
            <span>Managed devices as children</span>
            <Switch
              checked={!!data.promote_managed}
              disabled={promote.isPending}
              onCheckedChange={(on) => promote.mutate(on)}
            />
            {data.promote_managed && promotedCount > 0 && (
              <Link
                to={`/devices?managed_by=${deviceId}`}
                className="font-medium text-primary hover:underline"
              >
                {promotedCount} child device{promotedCount === 1 ? '' : 's'}
              </Link>
            )}
          </span>
        )}
      </div>

      {groups.length === 0 ? (
        <Card><CardContent className="p-6 text-center text-xs text-muted">
          Template attached — collecting vendor metrics… data appears within one or two SNMP polls.
        </CardContent></Card>
      ) : (
        /* One card width for every group, and rows that stretch so cards in a
         * row share a bottom edge. Letting wide tables span both columns was
         * worse than it looked: it produced two different card widths, and
         * because a span-2 card cannot start in the second column, whichever
         * card preceded it was left sitting alone against an empty half row. */
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

/* A row that has been materialized as a child device links to its page. */
function RowLabelText({ row }: { row: InsightRow }) {
  if (!row.child_device_id) return <>{row.label}</>
  return (
    <Link
      to={`/devices/${row.child_device_id}`}
      className="text-primary hover:underline"
      title={`Open the device page for ${row.label}`}
    >
      {row.label}
    </Link>
  )
}

function GroupStatusBadge({ status }: { status: string }) {
  if (status === 'crit') return <Badge variant="danger">Attention</Badge>
  if (status === 'warn') return <Badge variant="warning">Warning</Badge>
  if (status === 'ok') return <Badge variant="success">Healthy</Badge>
  return null
}

/* ── Shared pieces ───────────────────────────────────────────────── */

function GroupHeader({ title, count, status }: {
  title: string; count?: number; status: string
}) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {count != null && (
        <span className="text-[11px] text-muted">{count} row{count === 1 ? '' : 's'}</span>
      )}
      <GroupStatusBadge status={status} />
    </div>
  )
}

/* One KPI tile. Clickable only when there is a series to chart. */
function MetricTile({ label, value, status, onChart }: {
  label: string; value: string; status: string; onChart?: () => void
}) {
  const body = (
    <>
      <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wide text-muted">
        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', statusDot[status] || statusDot.none)} />
        <span className="truncate" title={label}>{label}</span>
        {onChart && <LineChartIcon className="ml-auto h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover/tile:opacity-60" />}
      </div>
      <div className={cn('mt-1 truncate text-base font-bold tabular-nums', statusText[status] || statusText.none)}>
        {value}
      </div>
    </>
  )
  const cls = 'group/tile rounded-lg border border-border bg-surface2 px-3 py-2.5 text-left'
  if (!onChart) return <div className={cls}>{body}</div>
  return (
    <button type="button" onClick={onChart} title="Show history"
      className={cn(cls, 'transition-colors hover:border-primary/50')}>
      {body}
    </button>
  )
}

/* Tiles wrap into as many columns as fit rather than a fixed 3. auto-fill
 * rather than auto-fit on purpose: auto-fit collapses the unused tracks and
 * stretches two tiles across an entire card, which is how the old fixed grid
 * ended up so airy. auto-fill keeps the tracks, so a tile is the same size
 * whether its group has two metrics or eight. */
function TileGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid auto-rows-fr grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-2">
      {children}
    </div>
  )
}

/* Label/value pairs for text metrics (firmware revisions, signature dates). */
function TextRows({ items, bordered }: {
  items: Array<{ key: string; name: string; text: string }>; bordered: boolean
}) {
  // A lone pair in a two-column grid puts its value at the halfway mark, which
  // reads as a stray floating value rather than a label/value row.
  return (
    <dl className={cn('grid grid-cols-1 gap-x-6 gap-y-1.5 text-xs',
      items.length > 1 && 'sm:grid-cols-2',
      bordered && 'mt-3 border-t border-border pt-3')}>
      {items.map((m) => (
        <div key={m.key} className="flex items-baseline justify-between gap-3 border-b border-border/30 pb-1 last:border-0">
          <dt className="shrink-0 text-muted">{m.name}</dt>
          <dd className="truncate text-right font-medium tabular-nums" title={m.text}>{m.text}</dd>
        </div>
      ))}
    </dl>
  )
}

/* ── Scalar group: KPI tile grid ─────────────────────────────────── */

function ScalarGroupCard({ group: g, onChart }: {
  group: InsightGroup; onChart: (m: InsightMetric) => void
}) {
  const metrics = (g.metrics || []).filter((m) => m.has_data)
  const numeric = metrics.filter((m) => m.type !== 'string')
  const strings = metrics.filter((m) => m.type === 'string' && m.text)
  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <GroupHeader title={g.name} status={g.status} />
        {numeric.length > 0 && (
          <TileGrid>
            {numeric.map((m) => (
              <MetricTile key={m.key} label={m.name} status={m.status}
                value={m.type === 'enum' && m.text ? m.text : formatValue(m.value, m.unit)}
                onChart={m.type === 'enum' ? undefined : () => onChart(m)} />
            ))}
          </TileGrid>
        )}
        {strings.length > 0 && (
          <TextRows items={strings.map((m) => ({ key: m.key, name: m.name, text: m.text }))}
            bordered={numeric.length > 0} />
        )}
      </CardContent>
    </Card>
  )
}

/* ── Narrow table group: label/value pairs flowed into columns ────── */

function CompactListGroup({ group: g, cols, rows, onChart }: {
  group: InsightGroup
  cols: InsightColumn[]
  rows: InsightRow[]
  onChart: (cell: InsightCell, row: InsightRow, col: InsightColumn) => void
}) {
  const [expanded, setExpanded] = useState(false)
  // Wrapping into columns makes a list far shorter, so show more before
  // collapsing than the table does — 18 fills three columns of six.
  const limit = 18
  const shown = expanded ? rows : rows.slice(0, limit)

  /* Column width follows the longest label. Fixed-width tracks packed three
   * across are right for "1" or "Fan 2", but chop "Switch 1 - HotSpot Temp
   * Sensor, GREEN" down to nothing; past a certain length one row per line is
   * the only way the name survives. */
  const maxLabel = rows.reduce((n, r) => Math.max(n, (r.label || '').length), 0)
  const track =
    maxLabel <= 10 ? '11rem'
    : maxLabel <= 22 ? '16rem'
    : maxLabel <= 32 ? '24rem'
    : '100%'
  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold">{g.name}</h3>
          <span className="text-[11px] text-muted">{rows.length} row{rows.length === 1 ? '' : 's'}</span>
          <GroupStatusBadge status={g.status} />
          {/* The column names live here rather than as table headers, which a
            * wrapped list has nowhere sensible to put. */}
          <span className="ml-auto text-[10px] uppercase tracking-wider text-muted">
            {cols.map((c) => c.name).join(' · ')}
          </span>
        </div>
        <div className="grid gap-x-6"
          style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${track}, 1fr))` }}>
          {shown.map((r) => (
            <div key={r.instance}
              className="flex items-baseline justify-between gap-3 border-b border-border/30 py-1.5 text-xs">
              <span className="min-w-0 flex-1 truncate font-medium" title={r.label}>
                <RowLabelText row={r} />
                {r.dupCount && <span className="ml-1.5 font-normal text-muted">×{r.dupCount}</span>}
              </span>
              {cols.map((c) => {
                const cell = r.cells[c.key]
                if (!cell) return <span key={c.key} className="shrink-0 text-muted">—</span>
                if (c.type === 'enum') {
                  return (
                    <span key={c.key}
                      className={cn('inline-flex shrink-0 items-center gap-1.5 font-medium', statusText[cell.status] || '')}>
                      <span className={cn('h-1.5 w-1.5 rounded-full', statusDot[cell.status] || statusDot.none)} />
                      {cell.text || cell.value}
                    </span>
                  )
                }
                if (c.type !== 'string' && cell.value != null) {
                  return (
                    <button key={c.key} type="button" title="Show history" onClick={() => onChart(cell, r, c)}
                      className={cn('shrink-0 cursor-pointer tabular-nums hover:underline', statusText[cell.status] || '')}>
                      {formatValue(cell.value, c.unit)}
                    </button>
                  )
                }
                return (
                  <span key={c.key} className="max-w-[8rem] shrink-0 truncate text-muted" title={cell.text}>
                    {cell.text || '—'}
                  </span>
                )
              })}
            </div>
          ))}
        </div>
        {rows.length > shown.length && !expanded && (
          <button type="button" onClick={() => setExpanded(true)}
            className="mt-2 flex items-center gap-1 text-[11px] text-primary hover:underline">
            <ChevronDown className="h-3 w-3" /> Show all {rows.length}
          </button>
        )}
        {expanded && rows.length > limit && (
          <button type="button" onClick={() => setExpanded(false)}
            className="mt-2 flex items-center gap-1 text-[11px] text-muted hover:underline">
            <ChevronRight className="h-3 w-3" /> Collapse
          </button>
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
  const cols = visibleColumns(g)
  const rows = collapseRepeatedRows(g.rows || [], cols)

  /* A one-row table is a set of readings about a single thing, not a list.
   * Rendered as a table it strings 2-4 values across the full card width with
   * nothing to scan against; as tiles it reads like every other scalar group. */
  if (rows.length === 1) {
    const r = rows[0]
    return (
      <Card className="h-full">
        <CardContent className="p-4">
          <GroupHeader title={g.name} status={g.status} />
          <TileGrid>
            {cols.map((c) => {
              const cell = r.cells[c.key]
              if (!cell) return null
              const chartable = c.type !== 'string' && c.type !== 'enum' && cell.value != null
              return (
                <MetricTile key={c.key} label={c.name} status={cell.status}
                  value={c.type === 'string' || c.type === 'enum'
                    ? (cell.text || '—')
                    : formatValue(cell.value, c.unit)}
                  onChart={chartable ? () => onChart(cell, r, c) : undefined} />
              )
            })}
          </TileGrid>
          {r.label && r.label !== r.instance && (
            <div className="mt-2 text-[11px] text-muted">{r.label}</div>
          )}
        </CardContent>
      </Card>
    )
  }

  /* One or two value columns make a very narrow table — 175px of content
   * adrift in a 748px card. Those read better as a wrapped list: the same
   * label/value pairs flowing into as many columns as the card fits, which
   * both fills the card and keeps each value beside its own label instead of
   * a card's width away from it. */
  if (cols.length <= 2) {
    return <CompactListGroup group={g} cols={cols} rows={rows} onChart={onChart} />
  }

  const shown = expanded ? rows : rows.slice(0, 8)
  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <GroupHeader title={g.name} count={rows.length} status={g.status} />
        {/* Three or more columns have enough of them to absorb the card width
         * without leaving the values stranded, so the table fills it. */}
        <div className="overflow-x-auto">
          <Table className="w-full">
            <THead>
              <Tr>
                <Th className="whitespace-nowrap py-2">Name</Th>
                {cols.map((c) => (
                  <Th key={c.key} className={cn('whitespace-nowrap py-2', alignOf(c.type))}>{c.name}</Th>
                ))}
              </Tr>
            </THead>
            <TBody>
              {shown.map((r) => (
                <Tr key={r.instance}>
                  <Td className="max-w-[220px] truncate p-2 text-xs font-medium" title={r.label}>
                    <RowLabelText row={r} />
                    {r.dupCount && (
                      <span className="ml-1.5 font-normal text-muted"
                        title={`The device reports ${r.dupCount} entries under this name with identical values`}>
                        ×{r.dupCount}
                      </span>
                    )}
                  </Td>
                  {cols.map((c) => {
                    const cell = r.cells[c.key]
                    if (!cell) return <Td key={c.key} className={cn('p-2 text-xs text-muted', alignOf(c.type))}>—</Td>
                    const isEnum = c.type === 'enum'
                    const isNum = c.type !== 'string' && cell.value != null
                    return (
                      <Td key={c.key} className={cn('whitespace-nowrap p-2 text-xs', alignOf(c.type))}>
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
                          <span className="block max-w-[200px] truncate text-muted" title={cell.text}>{cell.text || '—'}</span>
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
