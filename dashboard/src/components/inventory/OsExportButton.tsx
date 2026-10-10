import { useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { blobErrorMessage, downloadBlob, filenameFromDisposition } from '@/components/services/list/model'
import { serverNow } from '@/lib/serverClock'
import type { DownloadedFile } from '@/pages/inventory/api'
import { exportStartedText } from './assetModel'
import { fmtInt } from './model'

/**
 * The Export button of the Operating Systems, Hardware and Changes pages. It is only rendered for a user who
 * holds `inventory.export` (the caller decides); the file is the whole result of the same filters, not the
 * page, produced by the server (audit-logged there) and saved from the Blob. A file the server cut at its
 * row limit says so in the toast.
 */
export function ExportDatasetButton({
  run, filename, total, noun = 'rows', disabled, title,
}: {
  run: () => Promise<DownloadedFile>
  /** Name used when the server sends none. */
  filename: string
  /** What the list shows, for the toast when the server states no row count. */
  total?: number
  noun?: string
  disabled?: boolean
  title?: string
}) {
  const [busy, setBusy] = useState(false)
  async function onClick() {
    setBusy(true)
    try {
      const file = await run()
      const stamp = new Date(serverNow()).toISOString().slice(0, 10)
      downloadBlob(file.blob, filenameFromDisposition(file.disposition, `${filename}-${stamp}.csv`))
      const t = exportStartedText(total ?? 0, file)
      if (t.truncated) toast.warning(t.title, t.description)
      else toast.success(t.title, total === undefined && file.rows === null ? 'CSV file' : t.description)
    } catch (e) {
      toast.error('Export failed', await blobErrorMessage(e, 'The export could not be prepared'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="h-9"
      onClick={() => void onClick()}
      disabled={busy || disabled}
      aria-label="Export CSV"
      title={title ?? (total !== undefined ? `Export all ${fmtInt(total)} ${noun} as CSV` : 'Export as CSV')}
    >
      {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Download className="h-4 w-4" aria-hidden />}
      <span className="hidden sm:inline">Export</span>
    </Button>
  )
}
