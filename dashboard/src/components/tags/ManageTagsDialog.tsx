import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Pencil, Plus, Tags, Trash2, X } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { TAG_PALETTE, TagDef, autoTagColor, useTags } from '@/hooks/useTags'
import { TagBadge } from './TagBadge'

/** Registry management: create, rename (propagates to every device and
 * tag-scoped maintenance window), recolor, delete (strips from devices). */
export function ManageTagsDialog({
  open, onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const { data: defs, isLoading } = useTags(open)
  const [newName, setNewName] = useState('')

  function refresh() {
    qc.invalidateQueries({ queryKey: ['tags'] })
    qc.invalidateQueries({ queryKey: ['devices'] })
  }

  const create = useMutation({
    mutationFn: async (name: string) =>
      (await api.post('/tags', { name })).data as TagDef,
    onSuccess: (t) => { setNewName(''); refresh(); toast.success(`Tag “${t.name}” created`) },
    onError: (e: any) => toast.error('Create failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Tags className="h-5 w-5 text-primary" />
            Manage tags
          </DialogTitle>
          <DialogDescription>
            Renaming a tag updates it on every device and tag-scoped maintenance window.
            Deleting removes it from all devices.
          </DialogDescription>
        </DialogHeader>

        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            const name = newName.trim()
            if (name) create.mutate(name)
          }}
        >
          <Input
            placeholder="New tag name…"
            value={newName}
            maxLength={64}
            onChange={(e) => setNewName(e.target.value)}
          />
          <Button type="submit" disabled={!newName.trim() || create.isPending}>
            <Plus className="h-4 w-4" /> Add
          </Button>
        </form>

        <div className="divide-y divide-border rounded-md border border-border">
          {isLoading && <div className="p-4 text-sm text-muted">Loading…</div>}
          {!isLoading && (defs || []).length === 0 && (
            <div className="p-4 text-center text-sm text-muted">
              No tags yet. Create one above, or type one on any device.
            </div>
          )}
          {(defs || []).map((t) => (
            <TagRow key={t.id} tag={t} onChanged={refresh} />
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function TagRow({ tag, onChanged }: { tag: TagDef; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(tag.name)
  const [color, setColor] = useState(tag.color || autoTagColor(tag.name))
  const [confirmDelete, setConfirmDelete] = useState(false)

  const update = useMutation({
    mutationFn: async (patch: { name?: string; color?: string }) =>
      (await api.patch(`/tags/${tag.id}`, patch)).data as TagDef,
    onSuccess: () => { setEditing(false); onChanged() },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })
  const del = useMutation({
    mutationFn: async () => api.delete(`/tags/${tag.id}`),
    onSuccess: () => { onChanged(); toast.success(`Tag “${tag.name}” deleted`) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  if (editing) {
    return (
      <div className="space-y-2 p-3">
        <div className="flex items-center gap-2">
          <Input
            value={name}
            maxLength={64}
            onChange={(e) => setName(e.target.value)}
            className="h-8 flex-1 text-sm"
            autoFocus
          />
          <Button
            size="sm" className="h-8"
            disabled={!name.trim() || update.isPending}
            onClick={() => update.mutate({ name: name.trim(), color })}
          >
            <Check className="h-3.5 w-3.5" /> Save
          </Button>
          <Button
            variant="ghost" size="sm" className="h-8"
            onClick={() => { setEditing(false); setName(tag.name); setColor(tag.color || autoTagColor(tag.name)) }}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex items-center gap-1.5">
          {TAG_PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              title={c}
              onClick={() => setColor(c)}
              className={`h-5 w-5 rounded-full transition-transform hover:scale-110 ${
                color === c ? 'ring-2 ring-text ring-offset-1 ring-offset-surface' : ''
              }`}
              style={{ backgroundColor: c }}
            />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 p-3">
      <TagBadge name={tag.name} color={tag.color} />
      <span className="flex-1 truncate text-xs text-muted">
        {tag.device_count} device{tag.device_count === 1 ? '' : 's'}
        {tag.maintenance_count > 0 && (
          <span className="ml-2 inline-flex items-center gap-1 text-warning" title="Used by tag-scoped maintenance windows">
            <AlertTriangle className="h-3 w-3" />
            {tag.maintenance_count} maint.
          </span>
        )}
      </span>
      {confirmDelete ? (
        <>
          <span className="text-xs text-danger">Remove from {tag.device_count}?</span>
          <Button
            variant="destructive" size="sm" className="h-7"
            disabled={del.isPending}
            onClick={() => del.mutate()}
          >
            Delete
          </Button>
          <Button variant="ghost" size="sm" className="h-7" onClick={() => setConfirmDelete(false)}>
            <X className="h-3.5 w-3.5" />
          </Button>
        </>
      ) : (
        <>
          <Button
            variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-text"
            title="Edit" onClick={() => setEditing(true)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
            title="Delete" onClick={() => setConfirmDelete(true)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </>
      )}
    </div>
  )
}
