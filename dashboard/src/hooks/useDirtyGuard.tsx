import { useCallback, useState } from 'react'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

/** Protect an editing dialog from accidental dismissal (backdrop click, Escape,
 *  Close, Cancel). Wrap the dialog's `onOpenChange`:
 *
 *    const guard = useDirtyGuard(dirty, saving, onOpenChange)
 *    <Dialog open={open} onOpenChange={guard.onOpenChange}> … {guard.confirm}
 *
 *  A clean draft closes immediately; a changed one asks Keep editing / Discard;
 *  nothing can dismiss the dialog while a save is in flight. */
export function useDirtyGuard(dirty: boolean, busy: boolean, onOpenChange: (open: boolean) => void) {
  const [asking, setAsking] = useState(false)

  const handle = useCallback((open: boolean) => {
    if (open) { onOpenChange(true); return }
    if (busy) return
    if (dirty) { setAsking(true); return }
    onOpenChange(false)
  }, [dirty, busy, onOpenChange])

  const confirm = (
    <ConfirmDialog
      open={asking}
      onOpenChange={setAsking}
      title="Discard your changes?"
      description="You have unsaved changes in this form. Closing it now will lose them."
      confirmText="Discard changes"
      cancelText="Keep editing"
      destructive
      onConfirm={() => { setAsking(false); onOpenChange(false) }}
    />
  )
  return { onOpenChange: handle, confirm }
}
