import { useState } from 'react'
import { Download, FileText, FileSpreadsheet, Table2, Loader2, ChevronDown } from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { cn } from '@/lib/utils'

type ExportFormat = 'pdf' | 'excel' | 'csv'

interface ExportMenuProps {
  /** Maps to backend `report_type`. */
  reportType: 'executive_summary' | 'device_health' | 'service_health' | 'alert_analysis' | 'full_report'
  /** Active toolbar window. */
  fromISO: string
  toISO: string
  /** Tab name shown in the button. */
  label?: string
  disabled?: boolean
}

const FORMATS: { key: ExportFormat; label: string; ext: string; icon: typeof FileText; mime: string }[] = [
  { key: 'pdf', label: 'PDF', ext: 'pdf', icon: FileText, mime: 'application/pdf' },
  { key: 'excel', label: 'Excel', ext: 'xlsx', icon: FileSpreadsheet, mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  { key: 'csv', label: 'CSV', ext: 'csv', icon: Table2, mime: 'text/csv' },
]

export function ExportMenu({ reportType, fromISO, toISO, label = 'Export', disabled }: ExportMenuProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<ExportFormat | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function exportAs(fmt: ExportFormat) {
    if (busy) return
    setBusy(fmt)
    setError(null)
    try {
      const res = await api.post(
        '/reports/generate',
        {
          report_type: reportType,
          period: 'custom',
          from_time: fromISO,
          to_time: toISO,
          format: fmt,
        },
        { responseType: 'blob', timeout: 120_000 },
      )
      const formatMeta = FORMATS.find((f) => f.key === fmt)!
      const blob = new Blob([res.data], { type: formatMeta.mime })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
      a.download = `ZenPlus-${reportType}-${ts}.${formatMeta.ext}`
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
      <Button
        variant="outline"
        size="sm"
        disabled={disabled || !!busy}
        onClick={() => setOpen((v) => !v)}
        className="h-8 gap-1.5"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
        <span className="text-xs font-semibold">{busy ? `Exporting ${busy.toUpperCase()}…` : label}</span>
        <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </Button>

      {open && !busy && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-40 mt-1 w-56 overflow-hidden rounded-md border border-border bg-surface shadow-xl animate-fade-in">
            <div className="border-b border-border px-3 py-2">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted">Export current view</p>
            </div>
            {FORMATS.map((f) => (
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
            {error && (
              <div className="border-t border-border bg-rose-500/5 px-3 py-2 text-xs text-rose-400">{error}</div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
