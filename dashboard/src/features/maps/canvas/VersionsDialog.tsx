/* Map versions (F10): list the server-side snapshots of a map, take one on
 * demand and restore an older one. The server snapshots the current state
 * before every restore, so a restore can itself be undone from this list. */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Camera, History, Loader2, RefreshCw, RotateCcw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { formatAgo } from '../core'

export type MapVersion = {
  id: string
  created_at: string
  created_by?: string | null
  created_by_name?: string | null
  reason?: string | null
  node_count: number
  link_count: number
  shape_count: number
}

export type VersionsDialogProps = {
  mapId: string
  onClose: () => void
  /** Called after a successful restore (the map query is already invalidated). */
  onRestored: () => void
}

const inputCls = 'w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60'

export function versionsQueryKey(mapId: string) {
  return ['manual-map-versions', mapId] as const
}

export function VersionsDialog({ mapId, onClose, onRestored }: VersionsDialogProps) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState<MapVersion | null>(null)

  const versions = useQuery<{ data: MapVersion[] }>({
    queryKey: versionsQueryKey(mapId),
    queryFn: async () => (await api.get(`/maps/${mapId}/versions`)).data,
  })

  const snapshot = useMutation({
    mutationFn: async () =>
      (await api.post(`/maps/${mapId}/versions`, { reason: reason.trim() || null })).data as MapVersion,
    onSuccess: () => {
      setReason('')
      toast.success('Snapshot saved')
      qc.invalidateQueries({ queryKey: versionsQueryKey(mapId) })
    },
    onError: (e) => toast.error('Could not save snapshot', apiErrorMessage(e)),
  })

  const restore = useMutation({
    mutationFn: async (v: MapVersion) =>
      (await api.post(`/maps/${mapId}/versions/${v.id}/restore`)).data as { restored: boolean; backup_version_id?: string },
    onSuccess: (_r, v) => {
      setConfirm(null)
      toast.success('Map restored', `Back to ${new Date(v.created_at).toLocaleString()}. The previous state was saved as a version.`)
      qc.invalidateQueries({ queryKey: ['manual-map', mapId] })
      qc.invalidateQueries({ queryKey: ['manual-map-live', mapId] })
      qc.invalidateQueries({ queryKey: versionsQueryKey(mapId) })
      onRestored()
    },
    onError: (e) => toast.error('Restore failed', apiErrorMessage(e)),
  })

  const rows = versions.data?.data || []

  return (
    <>
      <Dialog open onOpenChange={(o) => { if (!o && !restore.isPending) onClose() }}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><History className="h-4 w-4 text-primary" /> Map versions</DialogTitle>
            <DialogDescription className="text-xs">
              Snapshots of nodes, links, shapes and map settings. Restoring saves the current state first, so it can be undone.
            </DialogDescription>
          </DialogHeader>

          {/* Save snapshot now */}
          <div className="flex items-end gap-2 rounded-lg border border-border bg-surface2/40 p-2.5">
            <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
              <span className="font-medium text-muted">Reason (optional)</span>
              <input
                className={inputCls}
                value={reason}
                maxLength={120}
                placeholder="e.g. before re-cabling core"
                onChange={(e) => setReason(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !snapshot.isPending) snapshot.mutate() }}
              />
            </label>
            <Button size="sm" onClick={() => snapshot.mutate()} disabled={snapshot.isPending}>
              {snapshot.isPending ? <Loader2 className="animate-spin" /> : <Camera />} Save snapshot now
            </Button>
          </div>

          {/* List */}
          <div className="flex items-center justify-between text-2xs font-bold uppercase tracking-wider text-muted">
            <span>History {rows.length > 0 && <span className="font-mono">({rows.length})</span>}</span>
            <button
              type="button"
              onClick={() => versions.refetch()}
              className="flex items-center gap-1 rounded px-1 py-0.5 normal-case tracking-normal transition hover:bg-primary/10 hover:text-text"
              title="Refresh"
            >
              <RefreshCw className={cn('h-3 w-3', versions.isFetching && 'animate-spin')} /> Refresh
            </button>
          </div>
          <div className="max-h-[50vh] min-h-[120px] overflow-y-auto rounded-lg border border-border">
            {versions.isLoading ? (
              <div className="flex h-28 items-center justify-center gap-2 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading versions…</div>
            ) : versions.isError && !versions.data ? (
              <div className="flex h-28 flex-col items-center justify-center gap-2 px-4 text-center text-xs text-danger">
                {apiErrorMessage(versions.error, 'Could not load versions')}
                <Button size="sm" variant="outline" onClick={() => versions.refetch()}>Retry</Button>
              </div>
            ) : rows.length === 0 ? (
              <div className="flex h-28 flex-col items-center justify-center gap-1 px-4 text-center text-xs text-muted">
                <History className="h-5 w-5 opacity-60" />
                No versions yet. Save a snapshot before larger changes — building or restoring a map saves one automatically.
              </div>
            ) : (
              <ul className="divide-y divide-border/60">
                {rows.map((v, i) => (
                  <li key={v.id} className="flex items-center gap-3 px-3 py-2 text-xs">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-text" title={v.created_at}>{new Date(v.created_at).toLocaleString()}</span>
                        <span className="text-2xs text-muted">{formatAgo(v.created_at)}</span>
                        {i === 0 && <span className="rounded bg-primary/10 px-1 text-2xs font-semibold uppercase text-primary">latest</span>}
                      </div>
                      <div className="truncate text-xs text-text2" title={v.reason || undefined}>
                        {v.reason || <span className="italic text-muted">No reason given</span>}
                      </div>
                      <div className="mt-0.5 font-mono text-2xs text-muted">
                        {v.node_count} nodes · {v.link_count} links · {v.shape_count} shapes
                        {(v.created_by_name || v.created_by) && <span className="font-sans"> · by {v.created_by_name || v.created_by}</span>}
                      </div>
                    </div>
                    <Button size="sm" variant="outline" onClick={() => setConfirm(v)} disabled={restore.isPending}>
                      <RotateCcw /> Restore
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!confirm}
        onOpenChange={(o) => { if (!o && !restore.isPending) setConfirm(null) }}
        title="Restore this version?"
        description={confirm ? (
          <>
            The map will be replaced by the snapshot from <b>{new Date(confirm.created_at).toLocaleString()}</b>
            {' '}({confirm.node_count} nodes, {confirm.link_count} links, {confirm.shape_count} shapes).
            The current state is saved as a new version first.
          </>
        ) : undefined}
        confirmText={restore.isPending ? 'Restoring…' : 'Restore'}
        loading={restore.isPending}
        onConfirm={() => { if (confirm) restore.mutate(confirm) }}
      />
    </>
  )
}
