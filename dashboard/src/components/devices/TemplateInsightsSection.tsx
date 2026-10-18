import { DeviceWidgetGrid, type DeviceLayoutContext } from './DeviceWidgetGrid'
import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, CircleSlash, LayoutTemplate, LineChart as LineChartIcon } from 'lucide-react'
import { autoDetectState, filterInsightRows, insightRowSeverity, readableInstanceLabel } from './templatePresentation'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { formatAbsolute, formatBytes, formatMegabytes, formatReading, MISSING } from '@/pages/device/format'
import { UsageBar } from '@/pages/device/KpiTile'

/* Vendor-template insights for one device: the groups declared by the
 * attached monitoring template, rendered from /devices/{id}/template-insights.
 * Scalar groups become KPI tiles (click = history in the Performance tab);
 * table groups become status tables (VPN tunnels, HA members, APs, pools…).
 *
 * The API marks groups that do not apply to this device (HA members on a
 * standalone unit: status "na") and blanks the gauges of rows whose own state
 * says they are offline (`suppressed`): those render as "—", never 0. */

type InsightMetric = {
  key: string; name: string; type: string; unit?: string | null
  thresholds?: { warn?: number; crit?: number; op?: string } | null
  value: number | null; text: string; status: string
  series_key: string; has_data: boolean
}
type InsightCell = { value: number | null; text: string; status: string; series_key: string; suppressed?: string }
type InsightRow = {
  instance: string; label: string; cells: Record<string, InsightCell>
  // Set on children-capable groups when this row has been materialized as a
  // child device (managed AP / switch promoted to its own device page).
  child_device_id?: string | null
  /** The row's own state says it is offline / not present. */
  absent?: boolean
  // Set by collapseRepeatedRows when several identical device rows were folded
  // into this one.
  dupCount?: number
}
type InsightColumn = { key: string; name: string; unit?: string | null; type: string }
export type InsightGroup = {
  key: string; name: string; kind: 'scalar' | 'table'
  description?: string | null; no_data_reason?: string; status: string
  applicable?: boolean; not_applicable_reason?: string | null
  children_capable?: boolean
  /** The template-declared vendor tab that shows the group (null = default vendor tab or Hardware). */
  tab?: string | null
  metrics?: InsightMetric[]
  columns?: InsightColumn[]
  rows?: InsightRow[]
}
export type Insights = {
  template: { id: string; name: string; vendor?: string | null } | null
  updated_at: string | null
  groups: InsightGroup[]
  tabs?: Array<{ id: string; label: string; groups: string[] }>
  // True when the template declares managed-device tables (APs, switches)
  // that can be promoted to child devices of this controller.
  children_capable?: boolean
  promote_managed?: boolean
}

/** A reading opened in the Performance tab. */
export type ChartRequest = { seriesKey: string; title: string; unit?: string | null; warn?: number | null; crit?: number | null }

const statusText: Record<string, string> = {
  ok: 'text-text', warn: 'text-warning', crit: 'text-danger', info: 'text-text', none: 'text-text', na: 'text-muted',
}
const statusDot: Record<string, string> = {
  ok: 'bg-success', warn: 'bg-warning', crit: 'bg-danger',
  info: 'bg-info', none: 'bg-muted/40', na: 'bg-transparent border border-border',
}

/** Groups that have something to show. */
export function shownGroups(groups: InsightGroup[]): InsightGroup[] {
  return groups.filter((g) => g.kind === 'table' ? (g.rows?.length || 0) > 0 : (g.metrics || []).some((m) => m.has_data))
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
      return cell && cell.value == null && !cell.suppressed && (cell.text || '') === r.label
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

/** A reading with its unit; unit-less sensor values take a unit from the row label. */
function formatValue(v: number | null, unit?: string | null, label?: string): string {
  return formatReading(v, unit, label)
}

/* Memory pools report used and free bytes; a used-% column is what an operator reads. */
function usedPctColumn(g: InsightGroup): { used: string; free?: string; total?: string } | null {
  const cols = g.columns || []
  const used = cols.find((c) => /^used$/i.test(c.name) && /bytes|^b$|mb/i.test(c.unit || ''))
  if (!used) return null
  const free = cols.find((c) => /^free$/i.test(c.name) && c.unit === used.unit)
  const total = cols.find((c) => /^(total|size|capacity)$/i.test(c.name) && c.unit === used.unit)
  return free || total ? { used: used.key, free: free?.key, total: total?.key } : null
}

function rowUsedPct(row: InsightRow, spec: { used: string; free?: string; total?: string }): number | null {
  const used = row.cells[spec.used]?.value
  if (used == null) return null
  let total: number | null = null
  if (spec.total) total = row.cells[spec.total]?.value ?? null
  else if (spec.free) {
    const free = row.cells[spec.free]?.value
    total = free == null ? null : used + free
  }
  return total ? (used / total) * 100 : null
}

export function TemplateInsightsSection({
  deviceId, rangeHours, initialGroup, vendor, deviceType, snmpHasData, groupFilter, title = 'Device metrics', section = 'metrics',
  onChart, emptyText, showPromote = false,
}: {
  deviceId: string; rangeHours: number; initialGroup?: string
  /** Whether the collector has any SNMP data for the device — explains a missing template. */
  snmpHasData?: boolean
  /** Which template groups this view shows (the vendor tab and the Hardware tab split them). */
  groupFilter?: (g: InsightGroup) => boolean
  title?: string
  /** Widget layout section (storage key suffix). */
  section?: string
  /** Open a reading's history (the Performance tab). */
  onChart?: (request: ChartRequest) => void
  emptyText?: string
  /** Offer the "child devices for managed APs and switches" switch (the vendor tab). */
  showPromote?: boolean
} & DeviceLayoutContext) {
  const [selectedGroup, setSelectedGroup] = useState(initialGroup || 'all')
  const [groupSearch, setGroupSearch] = useState('')
  const queryClient = useQueryClient()

  const { data, isLoading, isError } = useQuery<Insights>({
    queryKey: ['device', deviceId, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/template-insights`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })

  const promote = useMutation({
    mutationFn: async (on: boolean) =>
      (await api.put(`/devices/${deviceId}`, { promote_managed: on })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['device', deviceId] })
      queryClient.invalidateQueries({ queryKey: ['devices'] })
    },
  })

  const groups = useMemo(() => shownGroups(data?.groups || []).filter((g) => !groupFilter || groupFilter(g)), [data, groupFilter])

  if (isLoading) return <Card><CardContent className="p-4 text-sm text-muted">Loading device metrics…</CardContent></Card>
  if (isError && !data) return <Card><CardContent className="p-4 text-sm text-warning">Device metrics could not be loaded. They will retry automatically.</CardContent></Card>
  if (!data?.template) {
    const auto = autoDetectState(snmpHasData)
    return <Card><CardContent className="space-y-1 p-6 text-sm text-muted">
      <div className="font-medium text-text">Monitoring template: {auto.label}</div>
      {auto.reason && <div>{auto.reason}</div>}
      <div className="text-xs">Vendor-specific metrics appear here once a template is attached.</div>
    </CardContent></Card>
  }
  const activeGroup = selectedGroup === 'all' || groups.some(g => g.key === selectedGroup) ? selectedGroup : 'all'
  const promotedCount = groups
    .filter((g) => g.children_capable)
    .reduce((n, g) => n + (g.rows || []).filter((r) => r.child_device_id).length, 0)
  const chart = (request: ChartRequest) => onChart?.(request)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <LayoutTemplate className="h-4 w-4 text-primary" />
        <h2 className="text-base font-semibold">{title}</h2>
        <Badge variant="info">{data.template.name}</Badge>
        <span className="text-xs text-muted">{data.updated_at ? `Collected ${formatAbsolute(data.updated_at)}` : 'Waiting for the first template poll…'}</span>
        {data.children_capable && showPromote && (
          <label className="ml-auto inline-flex items-center gap-2 text-xs text-muted">
            <span>Create child devices for managed APs and switches</span>
            <Switch
              checked={!!data.promote_managed}
              disabled={promote.isPending}
              onCheckedChange={(on) => promote.mutate(on)}
              aria-describedby={`promote-help-${deviceId}`}
            />
            {data.promote_managed && promotedCount > 0 && (
              <Link to={`/devices?managed_by=${deviceId}`} className="font-medium text-primary hover:underline">
                {promotedCount} child device{promotedCount === 1 ? '' : 's'}
              </Link>
            )}
            <span id={`promote-help-${deviceId}`} className="sr-only">Each managed AP or switch gets its own device page, so alert rules, maintenance, tags and reports apply to it. A controller outage raises one root-cause alert; child alerts are suppressed.</span>
          </label>
        )}
      </div>

      <div className="device-metric-browser">
        {groups.length > 1 && <aside className="device-metric-navigation" aria-label="Metric group navigation">
          <label htmlFor={`metric-groups-${deviceId}-${section}`} className="mb-2 block text-xs font-semibold text-muted">Groups</label>
          <input id={`metric-groups-${deviceId}-${section}`} type="search" aria-label="Find a group" placeholder="Find a group…" value={groupSearch} onChange={event => setGroupSearch(event.target.value)} className="h-9 w-full rounded-lg border border-border bg-surface2/40 px-3 text-sm" />
          <nav aria-label="Metric groups">
            <button type="button" aria-pressed={activeGroup === 'all'} onClick={() => setSelectedGroup('all')} className="hover:bg-surface2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"><LayoutTemplate className="h-3.5 w-3.5 shrink-0" /><span className="flex-1">All groups</span><span className="text-muted">{groups.length}</span></button>
            {groups.filter(group => group.name.toLowerCase().includes(groupSearch.toLowerCase().trim())).map(group => <button type="button" key={group.key} aria-pressed={activeGroup === group.key} onClick={() => setSelectedGroup(group.key)} className="hover:bg-surface2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"><span className={cn('h-2 w-2 shrink-0 rounded-full', statusDot[group.status] || statusDot.none)} aria-hidden /><span className="min-w-0 flex-1 break-words">{group.name}</span><span className="sr-only">{group.status === 'crit' || group.status === 'warn' ? 'Needs attention' : group.status === 'ok' ? 'Healthy' : group.status === 'na' ? 'Not applicable' : ''}</span></button>)}
            {!groups.some(group => group.name.toLowerCase().includes(groupSearch.toLowerCase().trim())) && <p className="px-2 py-3 text-xs text-muted">No matching groups.</p>}
          </nav>
        </aside>}
        <div className="min-w-0 space-y-3">
          <p className="text-xs text-muted">{activeGroup === 'all' ? 'All collected readings' : groups.find(group => group.key === activeGroup)?.name} · Select a numeric reading to see its history in Performance.</p>
          {groups.length === 0 ? (
            <Card><CardContent className="p-6 text-center text-sm text-muted">{emptyText || 'Template attached — waiting for collected readings.'}</CardContent></Card>
          ) : (
            <DeviceWidgetGrid deviceId={deviceId} vendor={vendor || data.template.vendor || undefined} deviceType={deviceType} section={section}
              visibleWidgetIds={activeGroup === 'all' ? undefined : [activeGroup]}
              onStartEdit={() => { setSelectedGroup('all'); setGroupSearch('') }}
              widgets={groups.map(g => ({
                id: g.key, title: g.name,
                width: g.applicable === false ? 6 : (g.kind === 'table' ? (g.rows?.length || 0) > 1 : (g.metrics || []).filter(m => m.has_data).length > 2) ? 12 : 6,
                height: g.applicable === false ? 4 : g.kind === 'table' ? 12 : 7, minWidth: g.kind === 'table' ? 3 : 2, minHeight: 3,
                content: g.applicable === false
                  ? <NotApplicableCard group={g} onChart={chart} />
                  : g.kind === 'table'
                    ? <TableGroupCard group={g} onChart={(cell, row, col) => chart({ seriesKey: cell.series_key, title: `${col.name} — ${readableInstanceLabel(row.label)}`, unit: col.unit })} />
                    : <ScalarGroupCard group={g} onChart={m => chart({ seriesKey: m.series_key, title: m.name, unit: m.unit, warn: m.thresholds?.warn, crit: m.thresholds?.crit })} />,
              }))} />
          )}
        </div>
      </div>

      {(data.groups || []).some(g => g.no_data_reason && (!groupFilter || groupFilter(g))) && <details className="rounded-lg border border-border bg-surface p-3 text-sm text-muted">
        <summary className="cursor-pointer font-medium">Unavailable readings</summary>
        <ul className="mt-2 space-y-1">{data.groups.filter(g => g.no_data_reason && (!groupFilter || groupFilter(g))).map(g => <li key={g.key}>{g.name}: {g.no_data_reason}</li>)}</ul>
      </details>}
    </div>
  )
}

/* A row that has been materialized as a child device links to its page. */
function RowLabelText({ row }: { row: InsightRow }) {
  const label = readableInstanceLabel(row.label)
  if (!row.child_device_id) return <span>{label}{label !== row.label && <span className="sr-only"> (SNMP instance {row.label})</span>}</span>
  return (
    <Link to={`/devices/${row.child_device_id}`} className="text-primary hover:underline">{label}</Link>
  )
}

export function GroupStatusBadge({ status }: { status: string }) {
  if (status === 'crit') return <Badge variant="danger">Attention</Badge>
  if (status === 'warn') return <Badge variant="warning">Warning</Badge>
  if (status === 'ok') return <Badge variant="success">Healthy</Badge>
  if (status === 'na') return <Badge variant="outline"><CircleSlash className="h-3 w-3" aria-hidden />Not applicable</Badge>
  return null
}

/* ── Shared pieces ───────────────────────────────────────────────── */

function GroupHeader({ title, count, status, description }: {
  title: string; count?: number; status: string; description?: string | null
}) {
  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {count != null && <span className="text-xs text-muted">{count} {count === 1 ? 'row' : 'rows'}</span>}
        <GroupStatusBadge status={status} />
      </div>
      {description && <details className="mt-1 text-xs text-muted"><summary className="cursor-pointer font-medium hover:text-text">About these readings</summary><p className="mt-1 leading-relaxed">{description}</p></details>}
    </div>
  )
}

/* One KPI tile. Clickable only when there is a series to chart. */
function MetricTile({ label, value, status, onChart, footer }: {
  label: string; value: string; status: string; onChart?: () => void; footer?: ReactNode
}) {
  const body = (
    <>
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted">
        <span className={cn('h-2 w-2 shrink-0 rounded-full', statusDot[status] || statusDot.none)} aria-hidden />
        <span className="min-w-0 break-words">{label}</span>
        {onChart && <LineChartIcon className="ml-auto h-3.5 w-3.5 shrink-0 opacity-0 transition-opacity group-hover/tile:opacity-60" aria-hidden />}
      </div>
      <div className={cn('mt-1 break-words text-lg font-semibold tabular-nums', statusText[status] || statusText.none)}>{value}</div>
      {footer}
    </>
  )
  const cls = 'group/tile rounded-lg border border-border bg-surface2/50 px-3 py-2.5 text-left'
  if (!onChart) return <div className={cls}>{body}</div>
  return (
    <button type="button" onClick={onChart} aria-label={`${label}: ${value}. Show history`}
      className={cn(cls, 'transition-colors hover:border-primary/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary')}>
      {body}
    </button>
  )
}

/* Tiles wrap into as many columns as fit. auto-fill keeps the tracks, so a tile
 * is the same size whether its group has two metrics or eight. */
function TileGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid auto-rows-fr grid-cols-[repeat(auto-fill,minmax(min(100%,11rem),1fr))] gap-2">
      {children}
    </div>
  )
}

/* Label/value pairs for text metrics (firmware revisions, signature dates). */
function TextRows({ items, bordered }: {
  items: Array<{ key: string; name: string; text: string }>; bordered: boolean
}) {
  return (
    <dl className={cn('grid grid-cols-1 gap-x-6 gap-y-1.5 text-sm', items.length > 1 && 'sm:grid-cols-2', bordered && 'mt-3 border-t border-border pt-3')}>
      {items.map((m) => (
        <div key={m.key} className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-1 last:border-0">
          <dt className="shrink-0 text-muted">{m.name}</dt>
          <dd className="min-w-0 break-words text-right font-medium tabular-nums">{m.text}</dd>
        </div>
      ))}
    </dl>
  )
}

/* "Disk Used" + "Disk Capacity" in MB read as "15 GB of 440 GB" with a bar. */
function pairUsedCapacity(metrics: InsightMetric[]): Array<{ used: InsightMetric; cap: InsightMetric; base: string }> {
  const out: Array<{ used: InsightMetric; cap: InsightMetric; base: string }> = []
  for (const used of metrics) {
    const m = /^(.*?)\s*used$/i.exec(used.name.trim())
    if (!m || used.value == null) continue
    const base = m[1]
    const cap = metrics.find((c) => c !== used && c.unit === used.unit && c.value != null &&
      new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*(capacity|total|size)$`, 'i').test(c.name.trim()))
    if (cap && cap.value) out.push({ used, cap, base: base || 'Used' })
  }
  return out
}

function sizeText(v: number, unit?: string | null): string {
  const u = (unit || '').toLowerCase()
  if (u === 'mb') return formatMegabytes(v)
  if (u === 'bytes' || u === 'b') return formatBytes(v)
  if (u === 'kb') return formatBytes(v * 1024)
  return formatValue(v, unit)
}

/* ── Scalar group: KPI tile grid ─────────────────────────────────── */

function ScalarGroupCard({ group: g, onChart }: {
  group: InsightGroup; onChart: (m: InsightMetric) => void
}) {
  const metrics = (g.metrics || []).filter((m) => m.has_data)
  const pairs = pairUsedCapacity(metrics)
  const paired = new Set(pairs.flatMap((p) => [p.used.key, p.cap.key]))
  const numeric = metrics.filter((m) => m.type !== 'string' && !paired.has(m.key))
  const strings = metrics.filter((m) => m.type === 'string' && m.text)
  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <GroupHeader title={g.name} status={g.status} description={g.description} />
        {(numeric.length > 0 || pairs.length > 0) && (
          <TileGrid>
            {numeric.map((m) => (
              <MetricTile key={m.key} label={m.name} status={m.status}
                value={m.type === 'enum' && m.text ? m.text : formatValue(m.value, m.unit, m.name)}
                onChart={m.type === 'enum' || !m.series_key ? undefined : () => onChart(m)} />
            ))}
            {pairs.map(({ used, cap, base }) => {
              const pct = (used.value! / cap.value!) * 100
              return (
                <MetricTile key={used.key} label={base || 'Used'} status={used.status}
                  value={`${sizeText(used.value!, used.unit)} of ${sizeText(cap.value!, cap.unit)}`}
                  onChart={used.series_key ? () => onChart(used) : undefined}
                  footer={<div className="mt-1.5 flex items-center gap-2"><UsageBar pct={pct} warn={80} crit={90} /><span className="text-xs tabular-nums text-muted">{pct < 1 ? '<1' : Math.round(pct)}%</span></div>} />
              )
            })}
          </TileGrid>
        )}
        {strings.length > 0 && (
          <TextRows items={strings.map((m) => ({ key: m.key, name: m.name, text: m.text }))} bordered={numeric.length + pairs.length > 0} />
        )}
      </CardContent>
    </Card>
  )
}

/* ── A group that does not apply to this device ───────────────────── */

function NotApplicableCard({ group: g }: { group: InsightGroup; onChart: (r: ChartRequest) => void }) {
  const [open, setOpen] = useState(false)
  const count = g.kind === 'table' ? g.rows?.length || 0 : (g.metrics || []).filter((m) => m.has_data).length
  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <GroupHeader title={g.name} status="na" />
        <p className="text-sm text-muted">{g.not_applicable_reason || 'Not applicable to this device.'} It is left out of health and issues.</p>
        {count > 0 && (
          <button type="button" onClick={() => setOpen((v) => !v)} className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline" aria-expanded={open}>
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}{open ? 'Hide' : 'Show'} the raw readings ({count})
          </button>
        )}
        {open && (
          <div className="mt-2 opacity-80">
            {g.kind === 'table'
              ? <TableGroupCard group={{ ...g, status: 'na' }} onChart={() => undefined} bare />
              : <TextRows bordered={false} items={(g.metrics || []).filter((m) => m.has_data).map((m) => ({ key: m.key, name: m.name, text: m.text || formatValue(m.value, m.unit, m.name) }))} />}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Table group: rows with status cells ─────────────────────────── */

function CellValue({ cell, col, row, onChart }: { cell: InsightCell | undefined; col: InsightColumn; row: InsightRow; onChart: () => void }) {
  if (!cell) return <span className="text-muted">{MISSING}</span>
  if (cell.suppressed) {
    return <span className="text-muted" aria-label={cell.suppressed}>{MISSING}<span className="sr-only"> ({cell.suppressed})</span></span>
  }
  if (col.type === 'enum') {
    return (
      <span className={cn('inline-flex items-center gap-1.5 font-medium', statusText[cell.status] || '')}>
        <span className={cn('h-2 w-2 rounded-full', statusDot[cell.status] || statusDot.none)} aria-hidden />
        {cell.text || cell.value}
      </span>
    )
  }
  const isNum = col.type !== 'string' && cell.value != null
  if (!isNum) return <span className="text-muted">{cell.text || MISSING}</span>
  const text = formatValue(cell.value, col.unit, `${col.name} ${row.label}`)
  if (!cell.series_key) return <span className={cn('tabular-nums', statusText[cell.status] || '')}>{text}</span>
  return (
    <button type="button" onClick={onChart} aria-label={`${col.name} ${text}: show history`}
      className={cn('cursor-pointer tabular-nums hover:underline', statusText[cell.status] || '')}>
      {text}
    </button>
  )
}

function TableGroupCard({ group: g, onChart, bare = false }: {
  group: InsightGroup
  onChart: (cell: InsightCell, row: InsightRow, col: InsightColumn) => void
  bare?: boolean
}) {
  const [search, setSearch] = useState('')
  const [attentionOnly, setAttentionOnly] = useState(false)
  const [page, setPage] = useState(0)
  const cols = visibleColumns(g)
  const rows = collapseRepeatedRows(g.rows || [], cols)
  const usage = usedPctColumn(g)
  const offline = rows.filter((r) => r.absent).length

  /* A one-row table is a set of readings about a single thing, not a list:
   * render it as tiles like every other scalar group. */
  if (rows.length === 1 && !bare) {
    const r = rows[0]
    return (
      <Card className="h-full">
        <CardContent className="p-4">
          <GroupHeader title={g.name} status={g.status} description={g.description} />
          <TileGrid>
            {cols.map((c) => {
              const cell = r.cells[c.key]
              if (!cell) return null
              const chartable = c.type !== 'string' && c.type !== 'enum' && cell.value != null && !!cell.series_key
              return (
                <MetricTile key={c.key} label={c.name} status={cell.suppressed ? 'none' : cell.status}
                  value={cell.suppressed ? MISSING : c.type === 'string' || c.type === 'enum' ? (cell.text || MISSING) : formatValue(cell.value, c.unit, `${c.name} ${r.label}`)}
                  onChart={chartable ? () => onChart(cell, r, c) : undefined} />
              )
            })}
          </TileGrid>
          {r.label && r.label !== r.instance && <div className="mt-2 text-xs text-muted">{readableInstanceLabel(r.label)}</div>}
        </CardContent>
      </Card>
    )
  }

  const filtered = filterInsightRows(rows, search, attentionOnly)
  const pageSize = 12
  const lastPage = Math.max(0, Math.ceil(filtered.length / pageSize) - 1)
  const activePage = Math.min(page, lastPage)
  const shown = filtered.slice(activePage * pageSize, (activePage + 1) * pageSize)
  const attentionCount = rows.filter(row => insightRowSeverity(row) >= 3).length
  const body = (
    <>
      {!bare && <GroupHeader title={g.name} count={rows.length} status={g.status} description={g.description} />}
      {!bare && <div className="mb-3 flex flex-wrap items-center gap-3">
        <input type="search" aria-label={`Search ${g.name}`} placeholder={`Search ${g.name.toLowerCase()}…`} value={search} onChange={event => { setSearch(event.target.value); setPage(0) }} className="h-9 w-full rounded-lg border border-border bg-surface2 px-3 text-sm sm:w-72" />
        <label className="inline-flex cursor-pointer items-center gap-2 text-sm"><input type="checkbox" checked={attentionOnly} onChange={event => { setAttentionOnly(event.target.checked); setPage(0) }} />Needs attention ({attentionCount})</label>
        <span className="text-xs text-muted sm:ml-auto">Attention first · {filtered.length} matching{offline ? ` · readings of ${offline} offline ${offline === 1 ? 'row are' : 'rows are'} not reported` : ''}</span>
      </div>}
      <div className="max-w-full overflow-x-auto">
        <Table className="w-full">
          <THead>
            <Tr>
              <Th className="whitespace-nowrap py-2">Name</Th>
              {cols.map((c) => (
                <Th key={c.key} className={cn('whitespace-nowrap py-2', alignOf(c.type))}>{c.name}</Th>
              ))}
              {usage && <Th className="whitespace-nowrap py-2 text-left">Used %</Th>}
            </Tr>
          </THead>
          <TBody>
            {shown.map((r) => (
              <Tr key={r.instance}>
                <Td className="min-w-[180px] max-w-[360px] break-words p-2 text-sm font-medium">
                  <RowLabelText row={r} />
                  {r.dupCount && <span className="ml-1.5 font-normal text-muted">×{r.dupCount}<span className="sr-only"> identical entries under this name</span></span>}
                </Td>
                {cols.map((c) => (
                  <Td key={c.key} className={cn('whitespace-nowrap p-2 text-sm', alignOf(c.type), c.type === 'string' && 'whitespace-normal')}>
                    <CellValue cell={r.cells[c.key]} col={c} row={r} onChart={() => { const cell = r.cells[c.key]; if (cell) onChart(cell, r, c) }} />
                  </Td>
                ))}
                {usage && (() => {
                  const pct = rowUsedPct(r, usage)
                  return <Td className="w-40 p-2 text-sm">{pct == null ? MISSING : <div className="flex items-center gap-2"><UsageBar pct={pct} warn={85} crit={95} /><span className="w-10 shrink-0 text-right tabular-nums">{pct < 1 ? '<1' : Math.round(pct)}%</span></div>}</Td>
                })()}
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      {!filtered.length && <p className="py-6 text-center text-sm text-muted">No entries match these filters.</p>}
      {filtered.length > pageSize && <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3 text-sm">
        <span className="text-muted">{activePage * pageSize + 1}–{Math.min((activePage + 1) * pageSize, filtered.length)} of {filtered.length}</span>
        <div className="flex gap-2"><button type="button" disabled={!activePage} onClick={() => setPage(activePage - 1)} className="rounded border border-border px-3 py-1.5 disabled:opacity-40">Previous</button><button type="button" disabled={activePage === lastPage} onClick={() => setPage(activePage + 1)} className="rounded border border-border px-3 py-1.5 disabled:opacity-40">Next</button></div>
      </div>}
    </>
  )
  if (bare) return <div>{body}</div>
  return <Card className="h-full"><CardContent className="p-4">{body}</CardContent></Card>
}
