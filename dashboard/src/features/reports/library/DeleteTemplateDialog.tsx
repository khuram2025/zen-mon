/**
 * Delete an organisation template. When schedules deliver it (known from the
 * catalog, or reported by the server's 409) the confirmation offers to delete
 * the template together with those schedules (?force=true).
 */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CalendarClock, Loader2, Trash2 } from 'lucide-react'
import { describeFailure } from '@/lib/access'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { useDeleteTemplate } from '../api'
import { conflictScheduleCount, plural, type LibraryItem } from './model'

export interface DeleteTarget {
  item: LibraryItem
  /** Schedules that deliver it according to the catalog (0 = none known). */
  schedules: number
}

export function DeleteTemplateDialog({ target, onClose }: {
  target: DeleteTarget | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const del = useDeleteTemplate()
  // The server's answer when it reports schedules the catalog did not know about.
  const [conflict, setConflict] = useState<{ ref: string; schedules: number } | null>(null)

  const item = target?.item ?? null
  const fromServer = !!item && conflict?.ref === item.ref
  const schedules = fromServer ? conflict!.schedules : target?.schedules ?? 0
  const scheduleText = plural(schedules, 'schedule')

  const close = () => {
    if (del.isPending) return
    setConflict(null)
    del.reset()
    onClose()
  }

  const confirm = () => {
    if (!item?.customId) return
    const force = schedules > 0
    del.mutate({ id: item.customId, force }, {
      onSuccess: () => {
        if (force) qc.invalidateQueries({ queryKey: ['report-schedules'] })
        toast.success(
          'Template deleted',
          force ? `“${item.title}” and the ${scheduleText} that delivered it were deleted.` : `“${item.title}” was deleted.`,
        )
        setConflict(null)
        onClose()
      },
      onError: (e) => {
        const f = describeFailure(e)
        if (f.kind === 'conflict' && !force) {
          setConflict({ ref: item.ref, schedules: conflictScheduleCount(f.message) ?? Math.max(1, item.schedules) })
          return
        }
        toast.error('The template was not deleted', f.message)
      },
    })
  }

  return (
    <Dialog open={!!target} onOpenChange={(o) => { if (!o) close() }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-6">
            <AlertTriangle className="h-5 w-5 shrink-0 text-danger-fg" aria-hidden />
            {schedules > 0 ? 'Delete template and its schedules?' : 'Delete template?'}
          </DialogTitle>
          <DialogDescription>
            {item && (schedules > 0
              ? <>“{item.title}” is delivered by {scheduleText}. Deleting the template also deletes {schedules === 1 ? 'that schedule' : 'those schedules'}, so their recipients stop receiving it.</>
              : <>“{item.title}” will be deleted{item.visibility === 'shared' ? ' for everyone it is shared with' : ''}.</>)}
            {' '}This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {fromServer && (
          <p role="status" className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning-fg">
            <CalendarClock className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            Schedules started using this template after the library loaded, so it was not deleted. Confirm again to delete it together with its {scheduleText}, or keep it.
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close} disabled={del.isPending}>
            {schedules > 0 ? 'Keep template' : 'Cancel'}
          </Button>
          <Button type="button" variant="destructive" onClick={confirm} disabled={del.isPending}>
            {del.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Trash2 aria-hidden />}
            {schedules > 0 ? `Delete template and its ${scheduleText}` : 'Delete template'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
