import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  ArrowLeft,
  CalendarClock,
  Check,
  ChevronDown,
  Download,
  FileCode,
  FileText,
  Loader2,
  Search,
  Server,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { ReportScheduleFormDialog } from '@/components/forms/ReportScheduleFormDialog'
import { Button } from '@/components/ui/Button'
import { Skeleton } from '@/components/ui/Skeleton'

/* ------------------------------------------------------------------ */
/*  Types — GET /reports/render/{key}?format=json                      */
/* ------------------------------------------------------------------ */

type KpiAccent = 'primary' | 'success' | 'warning' | 'danger' | 'info'

interface RenderKpi {
  label: string
  value: string | number
  accent?: KpiAccent
  subtitle?: string
}

interface ChartSeries {
  kind: 'area' | 'bars' | 'donut'
  unit?: string
  color?: KpiAccent
  y_domain?: [number, number]
  points: { t?: string; v?: number; label?: string; value?: number }[]
}

interface RenderChart {
  title: string
  data_uri?: string
  series?: ChartSeries
}

type ColStyle = 'text' | 'num' | 'mono' | 'pct' | 'pct-bar' | 'status' | 'severity'

interface RenderTable {
  title?: string
  headers: string[]
  styles?: ColStyle[]
  rows: (string | number)[][]
}

interface RenderSection {
  id: string
  title: string
  description?: string
  half?: boolean
  kpis: RenderKpi[]
  charts: RenderChart[]
  tables: RenderTable[]
  notes: string[]
}

interface RenderReport {
  key: string
  title: string
  description: string
  from: string
  to: string
  period_label: string
  generated_label: string
  company_name: string
  sections: RenderSection[]
}

/* ------------------------------------------------------------------ */
/*  Shared chart chrome (matches the app's repaired chart styling)     */
/* ------------------------------------------------------------------ */

const ACCENT: Record<KpiAccent, string> = {
  primary: '#3b82f6',
  success: '#22c55e',
  warning: '#f59e0b',
  danger: '#ef4444',
  info: '#22d3ee',
}
const DONUT_COLORS = ['#3b82f6', '#22d3ee', '#22c55e', '#f59e0b', '#ef4444', '#64748b']
const TICK = { fill: '#94a3b8', fontSize: 10 }
const GRID = 'rgba(148,163,184,0.15)'
const TOOLTIP_STYLE = {
  background: '#0d121b',
  border: '1px solid #1e293b',
  borderRadius: 8,
  color: '#e5e7eb',
  fontSize: 12,
} as const

function timeTickFormatter(points: { t?: string }[]): (t: string) => string {
  const first = points[0]?.t
  const last = points[points.length - 1]?.t
  const spanH = first && last ? (new Date(last).getTime() - new Date(first).getTime()) / 3.6e6 : 24
  return (t: string) => {
    const d = new Date(t)
    return spanH <= 48
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
      : d.toLocaleDateString([], { month: '2-digit', day: '2-digit' })
  }
}

function TimeSeriesChart({ series }: { series: ChartSeries }) {
  const color = ACCENT[series.color || 'primary']
  const data = series.points.filter((p) => p.t != null)
  const fmt = timeTickFormatter(data)
  const common = (
    <>
      <CartesianGrid stroke={GRID} vertical={false} />
      <XAxis dataKey="t" tick={TICK} tickFormatter={fmt} axisLine={false} tickLine={false} minTickGap={40} />
      <YAxis
        tick={TICK}
        axisLine={false}
        tickLine={false}
        width={44}
        domain={series.y_domain ?? [0, 'auto']}
        tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v))}
      />
      <Tooltip
        contentStyle={TOOLTIP_STYLE}
        labelFormatter={(t) => new Date(String(t)).toLocaleString()}
        formatter={(v: number) => [`${Number(v).toLocaleString()} ${series.unit || ''}`.trim(), '']}
      />
    </>
  )
  return (
    <ResponsiveContainer width="100%" height={220}>
      {series.kind === 'bars' ? (
        <BarChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
          {common}
          <Bar dataKey="v" fill={color} radius={[2, 2, 0, 0]} maxBarSize={18} />
        </BarChart>
      ) : (
        <AreaChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={`grad-${series.color || 'primary'}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.28} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          {common}
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.6}
            fill={`url(#grad-${series.color || 'primary'})`}
          />
        </AreaChart>
      )}
    </ResponsiveContainer>
  )
}

function DonutChart({ series }: { series: ChartSeries }) {
  const data = series.points
    .filter((p) => p.label != null && (p.value ?? 0) > 0)
    .map((p) => ({ name: p.label!, value: p.value! }))
  const total = data.reduce((a, d) => a + d.value, 0) || 1
  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-muted">No data for this window</p>
  }
  return (
    <div className="flex items-center gap-5">
      <div className="h-[180px] w-[180px] shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              innerRadius={52}
              outerRadius={82}
              paddingAngle={2}
              strokeWidth={0}
            >
              {data.map((_, i) => (
                <Cell key={i} fill={DONUT_COLORS[i % DONUT_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              formatter={(v: number, name: string) => [
                `${Number(v).toLocaleString()} ${series.unit || ''} · ${((v / total) * 100).toFixed(1)}%`,
                name,
              ]}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="min-w-0 flex-1 space-y-1.5">
        {data.map((d, i) => (
          <div key={d.name} className="flex items-center gap-2 text-xs">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-sm"
              style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }}
            />
            <span className="truncate font-medium text-text2">{d.name}</span>
            <span className="ml-auto shrink-0 tabular-nums text-muted">
              {d.value.toLocaleString()} {series.unit || ''}
            </span>
            <span className="w-12 shrink-0 text-right tabular-nums text-text">
              {((d.value / total) * 100).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Dense, style-hinted tables                                         */
/* ------------------------------------------------------------------ */

const NUMERIC_CELL_RE = /^[\d,.\s%—-]+(ms|s|GB|MB|KB|B|bps|Kbps|Mbps|Gbps|min)?$/

const STATUS_TONES: Record<string, string> = {
  up: 'bg-success/15 text-success',
  online: 'bg-success/15 text-success',
  healthy: 'bg-success/15 text-success',
  success: 'bg-success/15 text-success',
  'on track': 'bg-success/15 text-success',
  completed: 'bg-success/15 text-success',
  down: 'bg-danger/15 text-danger',
  offline: 'bg-danger/15 text-danger',
  critical: 'bg-danger/15 text-danger',
  failed: 'bg-danger/15 text-danger',
  breaching: 'bg-danger/15 text-danger',
  degraded: 'bg-warning/15 text-warning',
  'at risk': 'bg-warning/15 text-warning',
  warning: 'bg-warning/15 text-warning',
  pending: 'bg-warning/15 text-warning',
  stale: 'bg-warning/15 text-warning',
  info: 'bg-info/15 text-info',
}

function StatusPill({ value }: { value: string }) {
  const tone = STATUS_TONES[value.trim().toLowerCase()] || 'bg-surface2 text-muted'
  if (!value.trim() || value.trim() === '—') return <span className="text-muted">—</span>
  return (
    <span className={cn('inline-block rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', tone)}>
      {value}
    </span>
  )
}

/** Inline percent bar; tone depends on whether high values are good or bad. */
function PctBarCell({ value, header }: { value: string; header: string }) {
  const num = parseFloat(value.replace(/[^\d.]/g, ''))
  if (Number.isNaN(num)) return <span className="text-muted">{value || '—'}</span>
  const h = header.toLowerCase()
  const badHigh = /used|utili|cpu|mem/.test(h)
  const goodHigh = /avail|uptime|budget|attain/.test(h)
  const tone = badHigh
    ? num >= 90 ? 'bg-danger' : num >= 75 ? 'bg-warning' : 'bg-primary'
    : goodHigh
      ? num >= 99 ? 'bg-success' : num >= 90 ? 'bg-warning' : 'bg-danger'
      : 'bg-primary/70'
  return (
    <span className="flex items-center justify-end gap-2">
      <span className="h-1.5 w-14 overflow-hidden rounded-full bg-surface2">
        <span className={cn('block h-full rounded-full', tone)} style={{ width: `${Math.min(100, num)}%` }} />
      </span>
      <span className="w-12 text-right tabular-nums">{value}</span>
    </span>
  )
}

function SectionTable({ table, dense }: { table: RenderTable; dense?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const COLLAPSE_AT = 12

  const styles: ColStyle[] = table.headers.map((_, i) => {
    if (table.styles?.[i]) return table.styles[i]
    if (i === 0) return 'text'
    const cells = table.rows.map((r) => String(r[i] ?? '').trim()).filter(Boolean)
    return cells.length > 0 && cells.every((c) => NUMERIC_CELL_RE.test(c)) ? 'num' : 'text'
  })
  const showRank = table.rows.length > 4
  const rows = expanded ? table.rows : table.rows.slice(0, COLLAPSE_AT)

  return (
    <div className="min-w-0">
      {table.title && (
        <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">{table.title}</p>
      )}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border bg-surface2/60 text-left">
              {showRank && <th className="w-8 px-2 py-1.5 font-semibold text-muted">#</th>}
              {table.headers.map((h, i) => (
                <th
                  key={i}
                  className={cn(
                    'whitespace-nowrap px-3 py-1.5 font-semibold text-text2',
                    (styles[i] === 'num' || styles[i] === 'pct' || styles[i] === 'pct-bar') && 'text-right',
                  )}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={table.headers.length + (showRank ? 1 : 0)}
                  className="px-3 py-5 text-center text-muted"
                >
                  No data for this window
                </td>
              </tr>
            ) : (
              rows.map((row, ri) => (
                <tr key={ri} className={cn('border-b border-border/40 last:border-0', ri % 2 === 1 && 'bg-surface2/25')}>
                  {showRank && <td className="px-2 py-1.5 tabular-nums text-muted">{ri + 1}</td>}
                  {row.map((cell, ci) => {
                    const s = styles[ci]
                    const v = String(cell ?? '')
                    return (
                      <td
                        key={ci}
                        className={cn(
                          'px-3 text-text2',
                          dense ? 'py-1' : 'py-1.5',
                          ci === 0 && 'font-medium text-text',
                          s === 'mono' && 'font-mono text-[11px]',
                          (s === 'num' || s === 'pct') && 'text-right tabular-nums',
                          s === 'pct-bar' && 'text-right',
                        )}
                      >
                        {s === 'status' || s === 'severity' ? (
                          <StatusPill value={v} />
                        ) : s === 'pct-bar' ? (
                          <PctBarCell value={v} header={table.headers[ci] || ''} />
                        ) : (
                          v || '—'
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {table.rows.length > COLLAPSE_AT && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-[11px] font-medium text-primary hover:underline"
        >
          {expanded ? 'Show fewer' : `Show all ${table.rows.length} rows`}
        </button>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Section card + layout                                              */
/* ------------------------------------------------------------------ */

function StatTile({ kpi }: { kpi: RenderKpi }) {
  const color = ACCENT[kpi.accent || 'primary']
  return (
    <div className="rounded-lg border border-border bg-surface px-3.5 py-2.5" style={{ borderTopWidth: 2, borderTopColor: color }}>
      <div className="text-lg font-semibold leading-tight tabular-nums" style={{ color }}>
        {kpi.value}
      </div>
      <div className="mt-0.5 truncate text-[10px] font-medium uppercase tracking-wide text-muted" title={kpi.label}>
        {kpi.label}
      </div>
      {kpi.subtitle && <div className="text-[10px] text-muted">{kpi.subtitle}</div>}
    </div>
  )
}

function SectionCard({ section, index }: { section: RenderSection; index: number }) {
  const kpisOnly =
    section.kpis.length > 0 && section.charts.length === 0 && section.tables.length === 0
  const multiTable = section.tables.length > 1 && section.tables.every((t) => t.headers.length <= 5)

  return (
    <section
      id={`sec-${section.id}`}
      className="scroll-mt-20 rounded-xl border border-border bg-surface"
    >
      <header className="flex items-baseline gap-2.5 border-b border-border/60 px-4 py-2.5">
        <span className="text-[11px] font-bold tabular-nums text-primary">{String(index).padStart(2, '0')}</span>
        <h2 className="text-sm font-semibold text-text">{section.title}</h2>
        {section.description && (
          <p className="hidden min-w-0 truncate text-xs text-muted sm:block">{section.description}</p>
        )}
      </header>
      <div className={cn('space-y-4 p-4', kpisOnly && 'py-3')}>
        {section.kpis.length > 0 && (
          <div className={cn('grid grid-cols-2 gap-2.5 sm:grid-cols-3', section.kpis.length >= 4 && 'lg:grid-cols-5')}>
            {section.kpis.map((kpi, i) => (
              <StatTile key={i} kpi={kpi} />
            ))}
          </div>
        )}

        {section.charts.map((chart, i) => (
          <figure key={i} className="min-w-0">
            {chart.title && (
              <figcaption className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
                {chart.title}
              </figcaption>
            )}
            {chart.series?.kind === 'donut' ? (
              <DonutChart series={chart.series} />
            ) : chart.series ? (
              <TimeSeriesChart series={chart.series} />
            ) : chart.data_uri ? (
              <div className="overflow-hidden rounded-lg border border-border bg-white p-2">
                <img src={chart.data_uri} alt={chart.title} className="mx-auto max-h-[280px] rounded bg-white" />
              </div>
            ) : null}
          </figure>
        ))}

        {section.tables.length > 0 && (
          <div className={cn('grid gap-4', multiTable && 'lg:grid-cols-2')}>
            {section.tables.map((table, i) => (
              <SectionTable key={i} table={table} dense={multiTable} />
            ))}
          </div>
        )}

        {section.notes.map((note, i) => (
          <p key={i} className="rounded-md border border-warning/30 bg-warning/10 px-3 py-1.5 text-xs text-warning">
            {note}
          </p>
        ))}
      </div>
    </section>
  )
}

/** Pair consecutive half-width sections into two-column rows. */
function layoutRows(sections: RenderSection[]): RenderSection[][] {
  const rows: RenderSection[][] = []
  let i = 0
  while (i < sections.length) {
    if (sections[i].half && sections[i + 1]?.half) {
      rows.push([sections[i], sections[i + 1]])
      i += 2
    } else {
      rows.push([sections[i]])
      i += 1
    }
  }
  return rows
}

/* ------------------------------------------------------------------ */
/*  Node scope picker (reports that support a device filter)           */
/* ------------------------------------------------------------------ */

interface DeviceLite {
  id: string
  hostname: string | null
  ip_address: string | null
}

function DeviceScopePicker({
  selected,
  onChange,
}: {
  selected: string[]
  onChange: (ids: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')

  const { data } = useQuery<DeviceLite[]>({
    queryKey: ['devices', 'report-picker'],
    queryFn: async () => {
      const res = (await api.get('/devices', { params: { limit: 200 } })).data
      return (res?.data ?? res ?? []) as DeviceLite[]
    },
    staleTime: 5 * 60_000,
  })

  const devices = data ?? []
  const filtered = q
    ? devices.filter(
        (d) =>
          (d.hostname || '').toLowerCase().includes(q.toLowerCase()) ||
          (d.ip_address || '').includes(q),
      )
    : devices
  const selectedSet = new Set(selected)

  const toggle = (id: string) => {
    const next = new Set(selectedSet)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onChange([...next])
  }

  return (
    <div className="relative">
      <Button
        variant="outline"
        size="sm"
        className={cn('h-8 gap-1.5', selected.length > 0 && 'border-primary/50 text-primary')}
        onClick={() => setOpen((v) => !v)}
      >
        <Server className="h-3.5 w-3.5" />
        <span className="text-xs font-semibold">
          {selected.length > 0 ? `${selected.length} node${selected.length === 1 ? '' : 's'}` : 'All nodes'}
        </span>
        <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </Button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-40 mt-1 w-80 overflow-hidden rounded-md border border-border bg-surface shadow-xl animate-fade-in">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                Report scope · {devices.length} nodes
              </p>
              {selected.length > 0 && (
                <button
                  onClick={() => onChange([])}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                >
                  <X className="h-3 w-3" /> All nodes
                </button>
              )}
            </div>
            <div className="border-b border-border p-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                <input
                  autoFocus
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Search hostname or IP…"
                  className="h-8 w-full rounded-md border border-border bg-surface2/40 pl-7 pr-2 text-xs text-text outline-none placeholder:text-muted focus:border-primary/60"
                />
              </div>
            </div>
            <div className="max-h-72 overflow-y-auto p-1">
              {filtered.length === 0 ? (
                <p className="px-3 py-4 text-center text-xs text-muted">No matching nodes</p>
              ) : (
                filtered.map((d) => {
                  const on = selectedSet.has(d.id)
                  return (
                    <button
                      key={d.id}
                      onClick={() => toggle(d.id)}
                      className={cn(
                        'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-surface2',
                        on && 'bg-primary/10',
                      )}
                    >
                      <span
                        className={cn(
                          'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                          on ? 'border-primary bg-primary text-white' : 'border-border',
                        )}
                      >
                        {on && <Check className="h-2.5 w-2.5" />}
                      </span>
                      <span className="min-w-0 flex-1 truncate font-medium text-text">
                        {d.hostname || d.ip_address}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-muted">
                        {(d.ip_address || '').split('/')[0]}
                      </span>
                    </button>
                  )
                })
              )}
            </div>
            <div className="border-t border-border px-3 py-1.5 text-[10px] text-muted">
              {selected.length > 0
                ? `Report scoped to ${selected.length} node(s)`
                : 'No selection — the report covers every monitored node'}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Export dropdown (PDF / HTML via the render endpoint)               */
/* ------------------------------------------------------------------ */

type RenderFormat = 'pdf' | 'html'

const RENDER_FORMATS: { key: RenderFormat; label: string; ext: string; icon: typeof FileText; mime: string }[] = [
  { key: 'pdf', label: 'Download PDF', ext: 'pdf', icon: FileText, mime: 'application/pdf' },
  { key: 'html', label: 'Download HTML', ext: 'html', icon: FileCode, mime: 'text/html' },
]

function filenameFromDisposition(header: string | undefined): string | null {
  if (!header) return null
  const m = header.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)
  return m ? decodeURIComponent(m[1]) : null
}

function RenderExportMenu({
  reportKey,
  customId,
  fromISO,
  toISO,
  deviceIds,
}: {
  reportKey: string
  customId: string | null
  fromISO: string
  toISO: string
  deviceIds?: string[]
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<RenderFormat | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function exportAs(fmt: RenderFormat) {
    if (busy) return
    setBusy(fmt)
    setError(null)
    try {
      const res = await api.get(`/reports/render/${reportKey}`, {
        params: {
          format: fmt,
          from: fromISO,
          to: toISO,
          ...(customId ? { custom_id: customId } : {}),
          ...(deviceIds && deviceIds.length > 0 ? { device_ids: deviceIds.join(',') } : {}),
        },
        responseType: 'blob',
        timeout: 120_000,
      })
      const meta = RENDER_FORMATS.find((f) => f.key === fmt)!
      const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
      const name =
        filenameFromDisposition(res.headers?.['content-disposition']) ||
        `ZenPlus-${reportKey}-${ts}.${meta.ext}`
      const blob = new Blob([res.data], { type: meta.mime })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 5000)
      setOpen(false)
    } catch (e) {
      console.error('Export failed', e)
      setError('Export failed. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="relative">
      <Button variant="outline" size="sm" disabled={!!busy} onClick={() => setOpen((v) => !v)} className="h-8 gap-1.5">
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
        <span className="text-xs font-semibold">{busy ? `Exporting ${busy.toUpperCase()}…` : 'Export'}</span>
        <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </Button>

      {open && !busy && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-40 mt-1 w-56 overflow-hidden rounded-md border border-border bg-surface shadow-xl animate-fade-in">
            <div className="border-b border-border px-3 py-2">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted">Export this report</p>
            </div>
            {RENDER_FORMATS.map((f) => (
              <button
                key={f.key}
                onClick={() => exportAs(f.key)}
                className="flex w-full items-center gap-2.5 px-3 py-2 text-sm text-text hover:bg-surface2"
              >
                <f.icon className="h-4 w-4 text-primary" />
                <span className="font-medium">{f.label}</span>
                <span className="ml-auto text-[10px] text-muted">.{f.ext}</span>
              </button>
            ))}
            {error && <div className="border-t border-border bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
          </div>
        </>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function SectionReportViewer() {
  const { key = '' } = useParams<{ key: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const customId = searchParams.get('custom_id')
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [scheduleOpen, setScheduleOpen] = useState(false)

  // Node scope (availability report): kept in the URL so views are shareable.
  const nodeIds = useMemo(
    () => (searchParams.get('nodes') || '').split(',').filter(Boolean),
    [searchParams],
  )
  const setNodeIds = (ids: string[]) => {
    const next = new URLSearchParams(searchParams)
    if (ids.length > 0) next.set('nodes', ids.join(','))
    else next.delete('nodes')
    setSearchParams(next, { replace: true })
  }

  const { data: catalog } = useQuery<{ types: { key: string; filterable?: string[] }[] }>({
    queryKey: ['reports', 'catalog'],
    queryFn: async () => (await api.get('/reports/catalog')).data,
    staleTime: 5 * 60_000,
  })
  const supportsDeviceFilter =
    catalog?.types.find((t) => t.key === key)?.filterable?.includes('devices') ?? false

  // Presets slide with the wall clock every minute; bucket the window to 5
  // minutes so a heavy report isn't refetched (and reset to skeletons) each
  // tick, and keep the previous render visible while a new one loads.
  const [fromISO, toISO] = useMemo(() => {
    const floor = (iso: string) => {
      const d = new Date(iso)
      d.setMinutes(Math.floor(d.getMinutes() / 5) * 5, 0, 0)
      return d.toISOString()
    }
    return [floor(range.fromISO), floor(range.toISO)]
  }, [range.fromISO, range.toISO])

  const { data, isLoading, error, isPlaceholderData } = useQuery<RenderReport>({
    queryKey: ['reports', 'render', key, customId ?? null, fromISO, toISO, nodeIds.join(',')],
    queryFn: async () =>
      (
        await api.get(`/reports/render/${key}`, {
          params: {
            format: 'json',
            from: fromISO,
            to: toISO,
            ...(customId ? { custom_id: customId } : {}),
            ...(nodeIds.length > 0 ? { device_ids: nodeIds.join(',') } : {}),
          },
          timeout: 120_000,
        })
      ).data,
    enabled: !!key,
    placeholderData: (prev) => prev,
    staleTime: 4 * 60_000,
    retry: 1,
  })

  const rows = useMemo(() => layoutRows(data?.sections ?? []), [data])
  let sectionNo = 0

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      {/* Report header */}
      <div className="rounded-xl border border-border bg-surface px-5 py-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <Link
              to="/reports"
              className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-text"
            >
              <ArrowLeft className="h-3 w-3" /> Report library
            </Link>
            <h1 className="mt-1 flex items-center gap-2 text-xl font-semibold tracking-tight">
              <FileText className="h-5 w-5 text-primary" />
              {data?.title || 'Report'}
            </h1>
            {data?.description && <p className="mt-0.5 text-sm text-muted">{data.description}</p>}
            {data && (
              <p className="mt-1 flex items-center gap-2 text-[11px] tabular-nums text-muted">
                {data.period_label} · Generated {data.generated_label} · {data.company_name}
                {isPlaceholderData && <Loader2 className="h-3 w-3 animate-spin text-primary" />}
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
            {supportsDeviceFilter && <DeviceScopePicker selected={nodeIds} onChange={setNodeIds} />}
            <RenderExportMenu
              reportKey={key}
              customId={customId}
              fromISO={fromISO}
              toISO={toISO}
              deviceIds={nodeIds}
            />
            <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={() => setScheduleOpen(true)}>
              <CalendarClock className="h-3.5 w-3.5" />
              <span className="text-xs font-semibold">Schedule…</span>
            </Button>
          </div>
        </div>

        {/* Contents */}
        {data && data.sections.length > 1 && (
          <nav className="mt-3 flex flex-wrap gap-1.5 border-t border-border/60 pt-3">
            {data.sections.map((s, i) => (
              <a
                key={s.id}
                href={`#sec-${s.id}`}
                className="rounded-full border border-border bg-surface2/60 px-2.5 py-1 text-[11px] font-medium text-text2 transition-colors hover:border-primary/50 hover:text-primary"
              >
                <span className="mr-1 tabular-nums text-muted">{String(i + 1).padStart(2, '0')}</span>
                {s.title}
              </a>
            ))}
          </nav>
        )}
      </div>

      {/* Body */}
      {isLoading ? (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[220px] rounded-xl" />
          ))}
        </div>
      ) : error || !data ? (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load report: {apiErrorMessage(error)}
        </div>
      ) : data.sections.length === 0 ? (
        <div className="rounded-xl border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
          This report has no sections for the selected window.
        </div>
      ) : (
        rows.map((row, ri) => (
          <div key={ri} className={cn('grid gap-4', row.length === 2 && 'lg:grid-cols-2')}>
            {row.map((section) => {
              sectionNo += 1
              return <SectionCard key={section.id} section={section} index={sectionNo} />
            })}
          </div>
        ))
      )}

      <ReportScheduleFormDialog
        open={scheduleOpen}
        onOpenChange={setScheduleOpen}
        schedule={null}
        prefill={{ report_type: key === 'custom' ? 'custom' : key, custom_report_id: customId }}
      />
    </div>
  )
}
