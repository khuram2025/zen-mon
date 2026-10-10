import { useSyncExternalStore, type ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Whether a media query matches now, and again when it changes (the facet pane is a drawer below `lg`). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === 'undefined' || !window.matchMedia) return () => undefined
      const m = window.matchMedia(query)
      m.addEventListener('change', onChange)
      return () => m.removeEventListener('change', onChange)
    },
    () => typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(query).matches,
    () => false,
  )
}

/**
 * A side sheet on Radix Dialog (the same pattern as the NetFlow and report-library drawers): focus is
 * trapped inside while it is open, Escape and the close button close it, and focus returns to where it
 * was (or wherever `onCloseAutoFocus` sends it: the row that opened the asset flyout).
 */
export function Sheet({
  open, onOpenChange, side = 'right', title, titleHidden, description, children, footer, className, width = 'max-w-[480px]',
  onCloseAutoFocus, headerExtra,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  side?: 'left' | 'right'
  /** The accessible name; always rendered, visibly unless `titleHidden`. */
  title: ReactNode
  titleHidden?: boolean
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  className?: string
  width?: string
  onCloseAutoFocus?: (e: Event) => void
  /** Shown under the title, inside the header (identity block of the flyout). */
  headerExtra?: ReactNode
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content
          {...(description ? {} : { 'aria-describedby': undefined })}
          onCloseAutoFocus={onCloseAutoFocus}
          className={cn(
            'fixed inset-y-0 z-50 flex w-full flex-col bg-surface shadow-xl focus:outline-none data-[state=open]:animate-fade-in',
            width,
            side === 'right' ? 'right-0 border-l border-border' : 'left-0 border-r border-border',
            className,
          )}
        >
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div className="min-w-0 flex-1">
              <DialogPrimitive.Title className={cn('text-base font-semibold text-text', titleHidden && 'sr-only')}>{title}</DialogPrimitive.Title>
              {description && <DialogPrimitive.Description className="mt-0.5 text-xs text-text2">{description}</DialogPrimitive.Description>}
              {headerExtra}
            </div>
            <DialogPrimitive.Close
              className="rounded-md p-1 text-text2 outline-none hover:bg-surface2 hover:text-text focus-visible:ring-2 focus-visible:ring-primary"
              aria-label="Close"
            >
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">{children}</div>
          {footer && <div className="border-t border-border px-4 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
