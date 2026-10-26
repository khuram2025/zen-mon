import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Archive, CheckCircle2, Clock, Download, Loader2, RefreshCw, RotateCcw, Save, Trash2, Upload,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import type { Backup, BackupSchedule } from './types'
import { bytes, storageError } from './storageMath'
import { STORAGE_KEY, useOverview } from './useStorage'

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { variant: 'success' | 'warning' | 'danger' | 'info' | 'default'; icon?: any }> = {
    completed: { variant: 'success', icon: CheckCircle2 },
    running: { variant: 'info', icon: Loader2 },
    failed: { variant: 'danger', icon: AlertTriangle },
  }
  const meta = map[status] || { variant: 'default' as const }
  const Icon = meta.icon
  return (
    <Badge variant={meta.variant}>
      {Icon && <Icon className={cn('h-3 w-3', status === 'running' && 'animate-spin')} />}
      {status}
    </Badge>
  )
}

export function BackupsSection() {
  const qc = useQueryClient()
  const { data: overview } = useOverview()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [includeCh, setIncludeCh] = useState(false)
  const [note, setNote] = useState('')
  const [schedule, setSchedule] = useState<BackupSchedule | null>(null)
  const [restoreTarget, setRestoreTarget] = useState<Backup | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Backup | null>(null)
  const [restoreComponents, setRestoreComponents] = useState<string[]>(['postgres', 'config'])
  const [restartOpen, setRestartOpen] = useState(false)

  const effectiveSchedule = schedule ?? overview?.backup_schedule ?? null
  const scheduleValid = !effectiveSchedule || (
    Number.isInteger(effectiveSchedule.hour_utc) && effectiveSchedule.hour_utc >= 0 && effectiveSchedule.hour_utc <= 23 &&
    Number.isInteger(effectiveSchedule.keep_last) && effectiveSchedule.keep_last >= 1 && effectiveSchedule.keep_last <= 30
  )

  const { data: backups, isError: backupsError, isLoading: backupsLoading } = useQuery<Backup[]>({
    queryKey: [...STORAGE_KEY, 'backups'],
    queryFn: async () => (await api.get('/system/storage/backups')).data,
    refetchInterval: (q) => {
      const rows = q.state.data as Backup[] | undefined
      const busy = rows?.some((b) => b.status === 'running' || b.last_restore_status === 'running')
      return busy ? 2500 : 30_000
    },
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: [...STORAGE_KEY, 'backups'] })

  const create = useMutation({
    mutationFn: async () =>
      (await api.post('/system/storage/backups', {
        include_clickhouse: includeCh, note: note || null,
      })).data,
    onSuccess: () => { setNote(''); toast.success('Backup started', 'Track progress below'); invalidate() },
    onError: (e: any) => toast.error('Backup failed to start', storageError(e)),
  })

  const restore = useMutation({
    mutationFn: async () =>
      (await api.post(`/system/storage/backups/${restoreTarget!.id}/restore`, {
        components: restoreComponents,
      })).data,
    onSuccess: () => {
      setRestoreTarget(null)
      toast.success('Restore started', 'Track progress in the table — restart services once it completes')
      invalidate()
    },
    onError: (e: any) => toast.error('Restore failed to start', storageError(e)),
  })

  const remove = useMutation({
    mutationFn: async () => (await api.delete(`/system/storage/backups/${deleteTarget!.id}`)).data,
    onSuccess: () => { setDeleteTarget(null); toast.success('Backup deleted'); invalidate() },
    onError: (e: any) => { setDeleteTarget(null); toast.error('Delete failed', storageError(e)) },
  })

  const saveSchedule = useMutation({
    mutationFn: async () => (await api.put('/system/storage/backups/schedule', effectiveSchedule)).data,
    onSuccess: () => {
      toast.success('Backup schedule saved')
      qc.invalidateQueries({ queryKey: [...STORAGE_KEY, 'overview'] })
    },
    onError: (e: any) => toast.error('Save failed', storageError(e)),
  })

  const restart = useMutation({
    mutationFn: async () => (await api.post('/system/storage/restart-services')).data,
    onSuccess: () => { setRestartOpen(false); toast.success('Services restarting', 'The dashboard may briefly disconnect') },
    onError: (e: any) => { setRestartOpen(false); toast.error('Restart failed', storageError(e)) },
  })

  async function download(b: Backup) {
    try {
      toast.info('Preparing download…', b.kind === 'full' ? 'Full backups can be large' : undefined)
      const res = await api.get(`/system/storage/backups/${b.id}/download`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `zenplus-backup-${b.id}.tar.gz`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error('Download failed', storageError(e))
    }
  }

  async function upload(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    try {
      await api.post('/system/storage/backups/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      toast.success('Backup imported')
      invalidate()
    } catch (e) {
      toast.error('Import failed', storageError(e))
    }
  }

  const anyRestoreDone = backups?.some((b) => b.last_restore_status === 'completed')

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Archive className="h-4 w-4 text-primary" /> Backup &amp; Restore
        </CardTitle>
        <p className="text-xs text-muted">
          Configuration backups include devices, users, settings and alert rules.
          Full backups also include monitoring history. Download backups to separate storage for recovery.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Create + upload */}
        <div className="flex flex-wrap items-end gap-3">
          <label className="grow text-xs text-text2">
            Note (optional)
            <Input className="mt-1 h-9" placeholder="e.g. before network re-design"
              value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} />
          </label>
          <label className="flex h-9 items-center gap-2 text-xs text-text2">
            <Switch checked={includeCh} onCheckedChange={setIncludeCh} />
            Include metrics data
          </label>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Archive className="h-4 w-4" />}
            Create backup
          </Button>
          <input ref={fileInputRef} type="file" accept=".tar.gz,.tgz,application/gzip" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
          <Button variant="outline" type="button" onClick={() => fileInputRef.current?.click()}>
            <Upload className="h-4 w-4" /> Import
          </Button>
        </div>

        {/* Schedule */}
        {effectiveSchedule && (
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Clock className="h-4 w-4 text-info" /> Scheduled backups
              </div>
              <Switch checked={effectiveSchedule.enabled}
                onCheckedChange={(v) => setSchedule({ ...effectiveSchedule, enabled: v })} />
            </div>
            {effectiveSchedule.enabled && (
              <div className="mt-3 flex flex-wrap items-end gap-3">
                <label className="text-xs text-text2">
                  Frequency
                  <div className="mt-1 w-28">
                    <Select value={effectiveSchedule.frequency}
                      onValueChange={(v) => setSchedule({ ...effectiveSchedule, frequency: v as 'daily' | 'weekly' })}>
                      <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="daily">Daily</SelectItem>
                        <SelectItem value="weekly">Weekly</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </label>
                {effectiveSchedule.frequency === 'weekly' && (
                  <label className="text-xs text-text2">
                    Day
                    <div className="mt-1 w-32">
                      <Select value={String(effectiveSchedule.weekday)}
                        onValueChange={(v) => setSchedule({ ...effectiveSchedule, weekday: Number(v) })}>
                        <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {WEEKDAYS.map((d, i) => <SelectItem key={d} value={String(i)}>{d}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </div>
                  </label>
                )}
                <label className="text-xs text-text2">
                  At
                  <div className="mt-1 flex items-center gap-1">
                    <Input type="number" min={0} max={23} className="h-8 w-16 text-right tabular-nums"
                      value={effectiveSchedule.hour_utc}
                      onChange={(e) => setSchedule({ ...effectiveSchedule, hour_utc: Number(e.target.value) })} />
                    <span className="text-muted">:00 UTC{Number.isInteger(effectiveSchedule.hour_utc) &&
                      ` (${new Date(Date.UTC(2000, 0, 1, effectiveSchedule.hour_utc)).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} local)`}</span>
                  </div>
                </label>
                <label className="text-xs text-text2">
                  Keep last
                  <Input type="number" min={1} max={30} className="mt-1 h-8 w-16 text-right tabular-nums"
                    value={effectiveSchedule.keep_last}
                    onChange={(e) => setSchedule({ ...effectiveSchedule, keep_last: Number(e.target.value) })} />
                </label>
                <label className="flex h-8 items-center gap-2 text-xs text-text2">
                  <Switch checked={effectiveSchedule.include_clickhouse}
                    onCheckedChange={(v) => setSchedule({ ...effectiveSchedule, include_clickhouse: v })} />
                  Include metrics data
                </label>
              </div>
            )}
            <div className="mt-2 flex justify-end">
              <Button size="sm" variant="outline" onClick={() => saveSchedule.mutate()} disabled={saveSchedule.isPending || !scheduleValid}>
                {saveSchedule.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                Save schedule
              </Button>
            </div>
          </div>
        )}

        {/* Backup list */}
        {backupsError ? <p role="alert" className="text-sm text-warning">Backup inventory is unavailable. Refresh before starting another backup.</p> : backupsLoading ? <p className="text-sm text-muted">Loading backups…</p> : (backups || []).length === 0 ? (
          <div className="rounded-lg border border-dashed border-border/60 p-6 text-center text-xs text-muted">
            No backups yet. Create one above — it takes a few seconds for a configuration
            backup.
          </div>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Created</Th>
                <Th>Type</Th>
                <Th>Status</Th>
                <Th className="text-right">Size</Th>
                <Th>Restore</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {backups!.map((b) => (
                <Tr key={b.id}>
                  <Td>
                    <div className="text-sm">{new Date(b.created_at).toLocaleString()}</div>
                    <div className="text-xs text-muted">
                      {relativeTime(b.created_at)} · by {b.created_by}
                      {b.note && <> · {b.note}</>}
                    </div>
                    {b.error && <div className="mt-0.5 max-w-md truncate text-xs text-danger" >Backup could not complete. Check capacity and contact support if this continues.</div>}
                  </Td>
                  <Td>
                    <Badge variant={b.kind === 'full' ? 'info' : 'default'}>
                      {b.kind === 'full' ? 'Full' : 'Config'}
                    </Badge>
                  </Td>
                  <Td><StatusBadge status={b.status} /></Td>
                  <Td className="text-right tabular-nums">{b.status === 'completed' ? bytes(b.size_bytes) : '—'}</Td>
                  <Td>
                    {b.last_restore_status ? (
                      <div>
                        <StatusBadge status={b.last_restore_status} />
                        {b.last_restore_error && (
                          <div className="mt-0.5 max-w-[16rem] truncate text-xs text-danger" >
                            Restore could not complete. Contact support before retrying.
                          </div>
                        )}
                      </div>
                    ) : <span className="text-xs text-muted">—</span>}
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" title="Download" disabled={b.status !== 'completed'}
                        onClick={() => download(b)}>
                        <Download className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" title="Restore" disabled={b.status !== 'completed' || b.last_restore_status === 'running'}
                        onClick={() => {
                          setRestoreComponents(b.include_clickhouse ? ['postgres', 'config', 'clickhouse_data'] : ['postgres', 'config'])
                          setRestoreTarget(b)
                        }}>
                        <RotateCcw className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" title="Delete" disabled={b.status === 'running'}
                        onClick={() => setDeleteTarget(b)}>
                        <Trash2 className="h-4 w-4 text-danger" />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}

        {anyRestoreDone && (
          <div className="flex items-center justify-between rounded-lg border border-info/30 bg-info/10 px-3 py-2">
            <p className="text-xs text-info">
              A restore has completed. Restart the application services so every component
              picks up the restored data and configuration.
            </p>
            <Button size="sm" variant="outline" onClick={() => setRestartOpen(true)}>
              <RefreshCw className="h-3.5 w-3.5" /> Restart services
            </Button>
          </div>
        )}

        {/* Restore dialog */}
        <Dialog open={restoreTarget !== null} onOpenChange={(o) => !o && setRestoreTarget(null)}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <AlertTriangle className="h-5 w-5 text-danger" /> Restore backup
              </DialogTitle>
            </DialogHeader>
            <DialogDescription>
              Restoring overwrites current data with the backup from{' '}
              <span className="font-medium text-text">{restoreTarget && new Date(restoreTarget.created_at).toLocaleString()}</span>.
              Select what to restore:
            </DialogDescription>
            <div className="space-y-2">
              {[
                { id: 'postgres', label: 'Devices, users, settings & alert rules', always: true },
                { id: 'config', label: 'Appliance configuration', always: true },
                { id: 'clickhouse_schema', label: 'Monitoring history structure', always: true },
                { id: 'clickhouse_data', label: 'Monitoring history (replaces existing records)', always: false },
              ].filter((c) => c.always || restoreTarget?.include_clickhouse).map((c) => (
                <label key={c.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--primary,#3b82f6)]"
                    checked={restoreComponents.includes(c.id)}
                    onChange={(e) =>
                      setRestoreComponents(e.target.checked
                        ? [...restoreComponents, c.id]
                        : restoreComponents.filter((x) => x !== c.id))
                    }
                  />
                  {c.label}
                </label>
              ))}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setRestoreTarget(null)}>Cancel</Button>
              <Button variant="destructive" disabled={restoreComponents.length === 0 || restore.isPending}
                onClick={() => restore.mutate()}>
                {restore.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                Restore
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <ConfirmDialog
          open={deleteTarget !== null}
          onOpenChange={(o) => !o && setDeleteTarget(null)}
          title="Delete backup?"
          description={`The backup from ${deleteTarget ? new Date(deleteTarget.created_at).toLocaleString() : ''} (${deleteTarget ? bytes(deleteTarget.size_bytes) : ''}) will be permanently removed.`}
          confirmText="Delete"
          destructive
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />

        <ConfirmDialog
          open={restartOpen}
          onOpenChange={setRestartOpen}
          title="Restart application services?"
          description="Monitoring services restart. Monitoring briefly pauses and the dashboard may disconnect."
          confirmText="Restart"
          loading={restart.isPending}
          onConfirm={() => restart.mutate()}
        />
      </CardContent>
    </Card>
  )
}
