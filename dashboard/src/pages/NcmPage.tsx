import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { FileCode, RefreshCw, Save, GitCompare, Eye, X } from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime, apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Textarea } from '@/components/ui/Textarea'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'

function DiffView({ diff }: { diff: string }) {
  const lines = diff ? diff.split('\n') : []
  if (!lines.length) return <div className="text-xs text-muted">Configs are identical.</div>
  return (
    <pre className="max-h-[420px] overflow-auto rounded-md border border-border bg-surface2/40 p-0 text-[11px] leading-relaxed">
      {lines.map((l, i) => {
        const cls =
          l.startsWith('+') && !l.startsWith('+++')
            ? 'bg-success/10 text-success'
            : l.startsWith('-') && !l.startsWith('---')
            ? 'bg-danger/10 text-danger'
            : l.startsWith('@@')
            ? 'text-primary'
            : 'text-muted'
        return (
          <div key={i} className={`whitespace-pre-wrap px-2 font-mono ${cls}`}>
            {l || ' '}
          </div>
        )
      })}
    </pre>
  )
}

export function NcmPage() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<any>(null)
  const [pasteFor, setPasteFor] = useState<any>(null)
  const [pasteText, setPasteText] = useState('')
  const [viewVersion, setViewVersion] = useState<any>(null)

  const { data: overview, isFetching, refetch } = useQuery<any>({
    queryKey: ['ncm', 'overview'],
    queryFn: async () => (await api.get('/ncm/overview')).data,
    refetchInterval: 30000,
  })
  const devices: any[] = overview?.data || []

  const { data: versions } = useQuery<any>({
    queryKey: ['ncm', 'configs', selected?.device_id],
    queryFn: async () => (await api.get(`/devices/${selected.device_id}/configs`)).data,
    enabled: !!selected,
  })
  const vlist: any[] = versions?.data || []

  const { data: diff } = useQuery<any>({
    queryKey: ['ncm', 'diff', selected?.device_id, vlist[0]?.id, vlist[1]?.id],
    queryFn: async () =>
      (await api.get(`/devices/${selected.device_id}/configs-diff?a=${vlist[1].id}&b=${vlist[0].id}`)).data,
    enabled: !!selected && vlist.length >= 2,
  })

  const { data: viewContent } = useQuery<any>({
    queryKey: ['ncm', 'view', viewVersion?.id],
    queryFn: async () => (await api.get(`/devices/${selected.device_id}/configs/${viewVersion.id}`)).data,
    enabled: !!viewVersion && !!selected,
  })

  const backup = useMutation({
    mutationFn: async () =>
      (await api.post(`/devices/${pasteFor.device_id}/config-backup`, { content: pasteText, source_note: 'manual paste' })).data,
    onSuccess: (d: any) => {
      toast.success(d.is_change ? 'Config version saved' : 'No change since last backup')
      qc.invalidateQueries({ queryKey: ['ncm'] })
      setPasteFor(null)
      setPasteText('')
    },
    onError: (e: any) => toast.error('Backup failed', apiErrorMessage(e)),
  })

  const coverage = overview ? Math.round((overview.backed_up / Math.max(1, overview.total_devices)) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 text-primary" /> Config Backup (NCM)
          </h1>
          <p className="text-xs text-muted">Versioned device configuration archive with change diffs</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()}>
          <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} /> Refresh
        </Button>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Devices</div><div className="text-2xl font-semibold">{overview?.total_devices ?? 0}</div></CardContent></Card>
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Backed up</div><div className="text-2xl font-semibold text-success">{overview?.backed_up ?? 0}</div></CardContent></Card>
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Coverage</div><div className="text-2xl font-semibold">{coverage}%</div></CardContent></Card>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Device</Th>
                  <Th>Type / Vendor</Th>
                  <Th>Versions</Th>
                  <Th>Last backup</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {devices.map((d) => (
                  <Tr key={d.device_id}>
                    <Td>
                      <div className="font-medium">{d.hostname}</div>
                      <div className="font-mono text-xs text-muted">{d.ip}</div>
                    </Td>
                    <Td className="text-xs text-muted">
                      {d.device_type || '—'}
                      {d.vendor ? ` · ${d.vendor}` : ''}
                    </Td>
                    <Td>{d.versions ? <Badge variant="info">{d.versions}</Badge> : <span className="text-xs text-muted">none</span>}</Td>
                    <Td className="text-xs text-muted">{d.last_capture ? `${relativeTime(d.last_capture)} · ${d.last_by}` : '—'}</Td>
                    <Td>
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="sm" onClick={() => setSelected(d)} disabled={!d.versions}>
                          <GitCompare className="h-3.5 w-3.5" /> History
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => { setPasteFor(d); setPasteText('') }}>
                          <Save className="h-3.5 w-3.5" /> Backup
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {!devices.length && (
                  <Tr><Td colSpan={5} className="py-10 text-center text-muted">No devices.</Td></Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardContent className="space-y-3 pt-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">{selected.hostname} — version history</h3>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setSelected(null)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="grid gap-3 md:grid-cols-[260px_1fr]">
              <div className="space-y-1">
                {vlist.map((v) => (
                  <div key={v.id} className="flex items-center justify-between rounded border border-border px-2 py-1 text-xs">
                    <div>
                      <div>{relativeTime(v.captured_at)}</div>
                      <div className="text-muted">{v.line_count} lines · {v.captured_by} · {v.hash}</div>
                    </div>
                    <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setViewVersion(v)}>
                      <Eye className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ))}
              </div>
              <div>
                <div className="mb-1 text-xs font-medium text-muted">
                  Diff (latest two versions){diff ? ` · +${diff.added} −${diff.removed}` : ''}
                </div>
                {vlist.length < 2 ? (
                  <div className="text-xs text-muted">Need at least 2 versions to diff.</div>
                ) : (
                  <DiffView diff={diff?.diff || ''} />
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <Dialog open={!!pasteFor} onOpenChange={(o) => !o && setPasteFor(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Backup config — {pasteFor?.hostname}</DialogTitle></DialogHeader>
          <p className="text-xs text-muted">Paste the device running-config. SSH auto-fetch arrives in a later slice.</p>
          <Textarea
            className="h-64 font-mono text-xs"
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            placeholder={'hostname ...\ninterface ...'}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setPasteFor(null)}>Cancel</Button>
            <Button disabled={!pasteText.trim() || backup.isPending} onClick={() => backup.mutate()}>Save version</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!viewVersion} onOpenChange={(o) => !o && setViewVersion(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Config · {viewVersion && relativeTime(viewVersion.captured_at)}</DialogTitle></DialogHeader>
          <pre className="max-h-[60vh] overflow-auto rounded-md border border-border bg-surface2/40 p-2 text-[11px] font-mono">
            {viewContent?.content || 'Loading…'}
          </pre>
        </DialogContent>
      </Dialog>
    </div>
  )
}
