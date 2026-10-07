import { useState } from 'react'
import { ChevronDown, Download, FileCode2, FileSpreadsheet, FileText, Loader2, Printer } from 'lucide-react'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { toast } from '@/components/ui/Toast'
import { downloadReport, type RenderParams } from '../api'

type FileFormat = 'pdf' | 'xlsx' | 'html'

const FORMATS: { key: FileFormat; label: string; hint: string; icon: typeof FileText; aliases: string[] }[] = [
  { key: 'pdf', label: 'PDF document', hint: 'Branded, paginated', icon: FileText, aliases: ['pdf'] },
  { key: 'xlsx', label: 'Excel workbook', hint: 'One sheet per table', icon: FileSpreadsheet, aliases: ['xlsx', 'excel'] },
  { key: 'html', label: 'HTML page', hint: 'Self-contained file', icon: FileCode2, aliases: ['html'] },
]

/** Server detail from a failed blob download (the body is a Blob, not JSON). */
export async function downloadErrorMessage(e: unknown): Promise<string> {
  const data = (e as { response?: { data?: unknown } })?.response?.data
  if (typeof Blob !== 'undefined' && data instanceof Blob) {
    try {
      const body = JSON.parse(await data.text())
      if (typeof body?.detail === 'string') return body.detail
      if (typeof body?.detail?.message === 'string') return body.detail.message
    } catch {
      /* not JSON */
    }
  }
  return apiErrorMessage(e, 'The export failed. Please try again.')
}

export interface ExportMenuProps {
  /** Current render parameters (period and scope); null while they are not known. */
  params: RenderParams | null
  /** Formats the template supports (catalog `formats`); defaults to PDF, Excel and HTML. */
  formats?: string[]
  onPrint: () => void
}

/** Export: branded PDF / Excel / HTML of exactly this period and scope, or print. Callers gate it on reports.export. */
export function ExportMenu({ params, formats, onPrint }: ExportMenuProps) {
  const [busy, setBusy] = useState<FileFormat | null>(null)
  const offered = FORMATS.filter((f) => !formats?.length || f.aliases.some((a) => formats.includes(a)))

  const run = async (fmt: FileFormat) => {
    if (!params || busy) return
    const meta = FORMATS.find((f) => f.key === fmt)!
    setBusy(fmt)
    toast.info(`Preparing ${meta.label.toLowerCase()}…`, 'Large reports can take a minute.')
    try {
      const name = await downloadReport(params, fmt)
      toast.success('Download ready', name)
    } catch (e) {
      toast.error(`Could not export ${meta.label.toLowerCase()}`, await downloadErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 gap-1.5" disabled={!params} aria-busy={!!busy}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Download className="h-3.5 w-3.5" aria-hidden />}
          <span className="text-xs font-semibold">{busy ? `Exporting ${busy === 'xlsx' ? 'Excel' : busy.toUpperCase()}…` : 'Export'}</span>
          <ChevronDown className="h-3 w-3 opacity-60" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-60">
        <DropdownMenuLabel>Download this period and scope</DropdownMenuLabel>
        {offered.map((f) => (
          <DropdownMenuItem key={f.key} disabled={!!busy} onSelect={() => void run(f.key)}>
            <f.icon className="text-primary" aria-hidden />
            <span className="flex-1">{f.label}</span>
            <span className="text-[10px] text-muted">{f.hint}</span>
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => window.setTimeout(onPrint, 60)}>
          <Printer aria-hidden />
          <span className="flex-1">Print or save as PDF</span>
          <span className="text-[10px] text-muted">Ctrl P</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
