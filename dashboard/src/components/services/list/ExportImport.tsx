import { useEffect, useRef, useState } from 'react'
import { CheckCircle2, Download, FileJson, FileSpreadsheet, Loader2, Upload, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { blobErrorMessage, browserTimezone, downloadBlob, filenameFromDisposition, isRouteMissing } from './model'

const REPORT_RANGES = [
  { hours: 24, label: 'Last 24 hours' },
  { hours: 24 * 7, label: 'Last 7 days' },
  { hours: 24 * 30, label: 'Last 30 days' },
]

const today = () => new Date(serverNow()).toISOString().slice(0, 10)

/** Export menu: portable configuration, legacy summary JSON and the fleet availability report. */
export function ExportMenu({ filterQuery, canImport, onImport }: {
  /** Current list filters as API query params (applied to the report where supported). */
  filterQuery: URLSearchParams
  canImport: boolean
  onImport: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)

  async function exportPortable() {
    setBusy('portable')
    try {
      const res = await api.get('/service-checks/export/json', { params: { format: 'portable' } })
      const body = res.data
      const legacy = Array.isArray(body)
      const count = legacy ? body.length : Array.isArray(body?.checks) ? body.checks.length : 0
      downloadBlob(
        new Blob([JSON.stringify(body ?? [], null, 2)], { type: 'application/json' }),
        filenameFromDisposition(res.headers?.['content-disposition'], `service-checks-portable-${today()}.json`),
      )
      if (legacy) toast.info('Exported summary format', 'This appliance does not support the portable definition schema yet; the summary export was downloaded instead.')
      else toast.success('Configuration exported', `${count} monitor${count === 1 ? '' : 's'} · secrets are not included`)
    } catch (e) {
      toast.error('Export failed', apiErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  async function exportSummary() {
    setBusy('summary')
    try {
      const res = await api.get('/service-checks/export/json')
      const payload = Array.isArray(res.data) ? res.data : res.data?.data ?? res.data
      downloadBlob(
        new Blob([JSON.stringify(payload ?? [], null, 2)], { type: 'application/json' }),
        `service-checks-${today()}.json`,
      )
      const n = Array.isArray(payload) ? payload.length : 0
      toast.success('Exported', `${n} check${n === 1 ? '' : 's'}`)
    } catch (e) {
      toast.error('Export failed', apiErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  async function exportReport(hours: number) {
    setBusy('report')
    const to = new Date(serverNow())
    const from = new Date(to.getTime() - hours * 3_600_000)
    const qs = new URLSearchParams(filterQuery)
    qs.set('format', 'csv')
    qs.set('from', from.toISOString())
    qs.set('to', to.toISOString())
    qs.set('timezone', browserTimezone())
    try {
      const res = await api.get(`/service-checks/report?${qs.toString()}`, { responseType: 'blob', timeout: 120_000 })
      const fallback = `service-availability-${from.toISOString().slice(0, 10)}-to-${to.toISOString().slice(0, 10)}.csv`
      downloadBlob(res.data as Blob, filenameFromDisposition(res.headers?.['content-disposition'], fallback))
      toast.success('Availability report downloaded', `${REPORT_RANGES.find((r) => r.hours === hours)?.label} · confirmed basis · ${browserTimezone()}`)
    } catch (e: any) {
      if (isRouteMissing(e) || e?.response?.status === 422) {
        // 422: an older appliance routes /report as a check id.
        toast.error('Report not available', 'This appliance version does not provide the fleet availability report.')
      } else {
        toast.error('Report failed', await blobErrorMessage(e, 'Could not build the report'))
      }
    } finally {
      setBusy(null)
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8" disabled={busy != null}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
          Export
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-72">
        <DropdownMenuLabel>Configuration</DropdownMenuLabel>
        <DropdownMenuItem onSelect={exportPortable}>
          <FileJson /> <span className="flex-1">Configuration (portable JSON)</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={exportSummary}>
          <FileJson /> <span className="flex-1">Summary (legacy JSON)</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Availability report (CSV, current filters)</DropdownMenuLabel>
        {REPORT_RANGES.map((r) => (
          <DropdownMenuItem key={r.hours} onSelect={() => exportReport(r.hours)}>
            <FileSpreadsheet /> {r.label}
          </DropdownMenuItem>
        ))}
        {canImport && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={onImport}>
              <Upload /> Import definitions…
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

type ImportItem = { name: string; action: string; error?: string | null }

function importItemsOf(res: any): ImportItem[] {
  const rows = Array.isArray(res) ? res : res?.results ?? res?.items ?? res?.data ?? res?.checks ?? []
  return Array.isArray(rows)
    ? rows.map((r: any) => ({ name: String(r?.name ?? '(unnamed)'), action: String(r?.action ?? (r?.error ? 'error' : 'unknown')), error: r?.error ?? null }))
    : []
}

const ACTION_LABEL: Record<string, string> = {
  create: 'Create',
  created: 'Created',
  skip_existing: 'Skip — name exists',
  error: 'Error',
}

/** Import portable definitions: dry-run preview first, then apply. */
export function ImportDialog({ open, onOpenChange, onImported }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onImported: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [doc, setDoc] = useState<unknown>(null)
  const [fileName, setFileName] = useState('')
  const [phase, setPhase] = useState<'pick' | 'previewing' | 'preview' | 'applying' | 'done'>('pick')
  const [items, setItems] = useState<ImportItem[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setDoc(null)
      setFileName('')
      setPhase('pick')
      setItems([])
      setError(null)
    }
  }, [open])

  async function onFile(f: File | undefined) {
    if (!f) return
    setError(null)
    setFileName(f.name)
    let parsed: any
    try {
      parsed = JSON.parse(await f.text())
    } catch {
      setError('The file is not valid JSON.')
      return
    }
    // A legacy summary export is a bare array; wrap it so the importer can say what it accepts.
    const body = Array.isArray(parsed) ? { schema_version: 1, checks: parsed } : parsed
    if (!body || !Array.isArray(body.checks)) {
      setError('Expected a portable export ({ "schema_version": 2, "checks": [...] }).')
      return
    }
    setDoc(body)
    setPhase('previewing')
    try {
      const res = (await api.post('/service-checks/import', body, { params: { dry_run: true }, timeout: 120_000 })).data
      setItems(importItemsOf(res))
      setPhase('preview')
    } catch (e) {
      setPhase('pick')
      setError(isRouteMissing(e) ? 'This appliance version does not support importing definitions.' : apiErrorMessage(e))
    }
  }

  async function apply() {
    if (!doc) return
    setPhase('applying')
    setError(null)
    try {
      const res = (await api.post('/service-checks/import', doc, { params: { dry_run: false }, timeout: 180_000 })).data
      setItems(importItemsOf(res))
      setPhase('done')
      onImported()
    } catch (e) {
      setPhase('preview')
      setError(apiErrorMessage(e))
    }
  }

  const creates = items.filter((i) => i.action === 'create' || i.action === 'created').length
  const skips = items.filter((i) => i.action === 'skip_existing').length
  const errors = items.filter((i) => i.action === 'error' || i.error).length

  return (
    <Dialog open={open} onOpenChange={(o) => { if (phase !== 'applying') onOpenChange(o) }}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{phase === 'done' ? 'Import results' : 'Import monitor definitions'}</DialogTitle>
          <DialogDescription>
            Upload a portable configuration export. A preview shows what would change before anything is created.
            Existing monitors with the same name are skipped; secrets are never imported — reattach credentials after import.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          {(phase === 'pick' || phase === 'previewing') && (
            <div className="flex flex-wrap items-center gap-2">
              <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" onChange={(e) => { void onFile(e.target.files?.[0]); e.target.value = '' }} />
              <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={phase === 'previewing'}>
                {phase === 'previewing' ? <Loader2 className="animate-spin" /> : <Upload />}
                {phase === 'previewing' ? 'Checking…' : 'Choose JSON file'}
              </Button>
              {fileName && <span className="truncate text-xs text-muted">{fileName}</span>}
            </div>
          )}

          {items.length > 0 && (
            <>
              <div className="flex flex-wrap gap-3 text-xs">
                <span className="text-success">{creates} {phase === 'done' ? 'created' : 'to create'}</span>
                <span className="text-muted">{skips} skipped (already exist)</span>
                <span className={errors ? 'text-danger' : 'text-muted'}>{errors} error{errors === 1 ? '' : 's'}</span>
              </div>
              <ul className="max-h-64 divide-y divide-border/60 overflow-y-auto rounded-md border border-border text-xs" aria-label="Import items">
                {items.map((i, n) => {
                  const bad = i.action === 'error' || !!i.error
                  return (
                    <li key={`${i.name}-${n}`} className="flex items-start gap-2 px-2.5 py-1.5">
                      {bad
                        ? <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
                        : <CheckCircle2 className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', i.action === 'skip_existing' ? 'text-muted' : 'text-success')} />}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium">{i.name}</span>
                        {i.error && <span className="block break-words text-danger">{i.error}</span>}
                      </span>
                      <span className="shrink-0 text-muted">{ACTION_LABEL[i.action] || i.action}</span>
                    </li>
                  )
                })}
              </ul>
            </>
          )}
          {phase === 'preview' && items.length === 0 && <p className="text-xs text-muted">The file contains no monitors.</p>}
          {error && <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{error}</p>}
        </div>

        <DialogFooter className="flex-wrap">
          {phase === 'done' ? (
            <Button onClick={() => onOpenChange(false)}>Close</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={phase === 'applying'}>Cancel</Button>
              {(phase === 'preview' || phase === 'applying') && (
                <Button onClick={apply} disabled={phase === 'applying' || creates === 0}>
                  {phase === 'applying' && <Loader2 className="animate-spin" />}
                  Import {creates} monitor{creates === 1 ? '' : 's'}
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
