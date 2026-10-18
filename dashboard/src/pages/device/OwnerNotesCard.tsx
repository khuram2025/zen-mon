import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Pencil, User } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Panel } from './charts'
import type { DeviceRecord } from './types'

/* Who looks after the device and what an operator should know before touching
 * it (circuit IDs, runbook pointers, change windows). Free text on the device
 * record; editing needs devices.manage, reading only the device. */

export const NOTES_MAX = 20000

export function OwnerNotesCard({ device }: { device: DeviceRecord }) {
  const canEdit = useCan()('devices.manage')
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [owner, setOwner] = useState('')
  const [notes, setNotes] = useState('')
  const save = useMutation({
    mutationFn: async () => (await api.put(`/devices/${device.id}`, { owner, notes })).data,
    onSuccess: (updated: DeviceRecord) => {
      qc.setQueryData(['device', device.id], (old: DeviceRecord | undefined) => (old ? { ...old, owner: updated.owner, notes: updated.notes } : old))
      qc.invalidateQueries({ queryKey: ['device', device.id] })
      setEditing(false)
      toast.success('Owner and notes saved')
    },
    onError: (e: unknown) => toast.error('Could not save owner and notes', apiErrorMessage(e)),
  })
  const start = () => { setOwner(device.owner || ''); setNotes(device.notes || ''); setEditing(true) }
  const edit = canEdit && !editing ? <button type="button" onClick={start} className="inline-flex items-center gap-1 text-muted hover:text-text"><Pencil className="h-3.5 w-3.5" />Edit</button> : undefined

  return (
    <Panel title="Owner and notes" meta="What to know before working on this device" actions={edit}>
      {editing ? (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
          <label className="block text-xs text-muted">Owner
            <input value={owner} onChange={(e) => setOwner(e.target.value)} maxLength={255} placeholder="Person or team, e.g. Network operations"
              className="mt-1 h-9 w-full rounded-lg border border-border bg-surface2 px-3 text-sm text-text" />
          </label>
          <label className="block text-xs text-muted">Notes
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={NOTES_MAX} rows={5}
              placeholder="Circuit IDs, runbook links, change windows, contacts…"
              className="mt-1 w-full rounded-lg border border-border bg-surface2 px-3 py-2 text-sm text-text" />
          </label>
          <div className="flex items-center justify-end gap-2">
            <Button type="button" size="sm" variant="outline" onClick={() => setEditing(false)} disabled={save.isPending}>Cancel</Button>
            <Button type="submit" size="sm" disabled={save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</Button>
          </div>
        </form>
      ) : (
        <div className="space-y-3 text-sm">
          <div className="flex items-start gap-2">
            <User className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
            <div><div className="text-xs text-muted">Owner</div><div className="font-medium">{device.owner || <span className="font-normal text-muted">Not recorded</span>}</div></div>
          </div>
          {device.notes
            ? <p className="whitespace-pre-wrap break-words">{device.notes}</p>
            : <p className="text-muted">No notes yet.{canEdit ? ' Add circuit IDs, runbook links or change windows with Edit.' : ''}</p>}
        </div>
      )}
    </Panel>
  )
}
