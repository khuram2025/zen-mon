import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeft,
  CalendarClock,
  ChevronDown,
  Download,
  FileCode,
  FileText,
  Loader2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { KpiTile } from '@/components/reports/KpiTile'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { Button } from '@/components/ui/Button'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { ReportScheduleFormDialog } from '@/components/forms/ReportScheduleFormDialog'

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

interface RenderChart {
  title: string
  data_uri: string
}

interface RenderTable {
  title?: string
  headers: string[]
  rows: string[][]
}

interface RenderSection {
  id: string
  title: string
  description?: string
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
}: {
  reportKey: string
  customId: string | null
  fromISO: string
  toISO: string
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
/*  Table rendering                                                    */
/* ------------------------------------------------------------------ */

const NUMERIC_CELL_RE = /^[\d,.\s%—-]+(ms|s|GB|MB|KB|B|bps|Kbps|Mbps|Gbps|min)?$/

/** Per-column alignment: first column left, others right when every value looks numeric. */
function columnAlignRight(table: RenderTable): boolean[] {
  return table.headers.map((_, idx) => {
    if (idx === 0) return false
    const cells = table.rows.map((r) => (r[idx] ?? '').trim()).filter((c) => c !== '')
    if (cells.length === 0) return false
    return cells.every((c) => NUMERIC_CELL_RE.test(c))
  })
}

function SectionTable({ table }: { table: RenderTable }) {
  const alignRight = columnAlignRight(table)
  return (
    <div>
      {table.title && <p className="mb-1.5 text-xs font-semibold text-text2">{table.title}</p>}
      <div className="overflow-hidden rounded-lg border border-border">
        <Table>
          <THead className="bg-surface2/60">
            <Tr>
              {table.headers.map((h, i) => (
                <Th key={i} className={cn(alignRight[i] && 'text-right')}>
                  {h}
                </Th>
              ))}
            </Tr>
          </THead>
          <TBody>
            {table.rows.length === 0 ? (
              <Tr>
                <Td colSpan={Math.max(1, table.headers.length)} className="py-6 text-center text-sm text-muted">
                  No data for this window
                </Td>
              </Tr>
            ) : (
              table.rows.map((row, ri) => (
                <Tr key={ri}>
                  {row.map((cell, ci) => (
                    <Td key={ci} className={cn('text-sm text-text2', alignRight[ci] && 'text-right tabular-nums')}>
                      {cell}
                    </Td>
                  ))}
                </Tr>
              ))
            )}
          </TBody>
        </Table>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function SectionReportViewer() {
  const { key = '' } = useParams<{ key: string }>()
  const [searchParams] = useSearchParams()
  const customId = searchParams.get('custom_id')
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [scheduleOpen, setScheduleOpen] = useState(false)

  const { data, isLoading, error } = useQuery<RenderReport>({
    queryKey: ['reports', 'render', key, customId ?? null, range.fromISO, range.toISO],
    queryFn: async () =>
      (
        await api.get(`/reports/render/${key}`, {
          params: {
            format: 'json',
            from: range.fromISO,
            to: range.toISO,
            ...(customId ? { custom_id: customId } : {}),
          },
          timeout: 120_000,
        })
      ).data,
    enabled: !!key,
  })

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <Link
            to="/reports"
            className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Report library
          </Link>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileText className="h-6 w-6 text-primary" />
            {data?.title || 'Report'}
          </h1>
          {data?.description && <p className="mt-1 text-sm text-muted">{data.description}</p>}
          {data && (
            <p className="mt-1 text-xs text-muted">
              {data.period_label} · Generated {data.generated_label}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <TimeRangePicker
            rangeIdx={rangeIdx}
            isCustom={isCustom}
            customFrom={range.fromISO}
            customTo={range.toISO}
            onPreset={setPreset}
            onCustom={setCustom}
          />
          <RenderExportMenu reportKey={key} customId={customId} fromISO={range.fromISO} toISO={range.toISO} />
          <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={() => setScheduleOpen(true)}>
            <CalendarClock className="h-3.5 w-3.5" />
            <span className="text-xs font-semibold">Schedule…</span>
          </Button>
        </div>
      </div>

      {/* Body */}
      {isLoading ? (
        <div className="space-y-5">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[240px] rounded-lg" />
          ))}
        </div>
      ) : error || !data ? (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load report: {apiErrorMessage(error)}
        </div>
      ) : data.sections.length === 0 ? (
        <ReportSection title={data.title}>
          <EmptyReportState message="This report has no sections for the selected window." />
        </ReportSection>
      ) : (
        data.sections.map((section) => (
          <ReportSection key={section.id} title={section.title} description={section.description}>
            <div className="space-y-4">
              {section.kpis.length > 0 && (
                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
                  {section.kpis.map((kpi, i) => (
                    <KpiTile
                      key={i}
                      label={kpi.label}
                      value={kpi.value}
                      accent={kpi.accent}
                      subtitle={kpi.subtitle}
                    />
                  ))}
                </div>
              )}

              {section.charts.length > 0 && (
                <div className={cn('grid gap-4', section.charts.length > 1 && 'lg:grid-cols-2')}>
                  {section.charts.map((chart, i) => (
                    <figure key={i} className="min-w-0">
                      <figcaption className="mb-1.5 text-xs font-semibold text-text2">{chart.title}</figcaption>
                      {/* Server-side charts are white-background PNGs — a white card keeps them intentional in dark theme. */}
                      <div className="overflow-hidden rounded-lg border border-border bg-white p-2">
                        <img src={chart.data_uri} alt={chart.title} className="w-full rounded bg-white" />
                      </div>
                    </figure>
                  ))}
                </div>
              )}

              {section.tables.map((table, i) => (
                <SectionTable key={i} table={table} />
              ))}

              {section.notes.map((note, i) => (
                <div key={i} className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
                  {note}
                </div>
              ))}
            </div>
          </ReportSection>
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
