import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogPortal = DialogPrimitive.Portal
export const DialogClose = DialogPrimitive.Close

export const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-50 bg-black/70 backdrop-blur-sm data-[state=open]:animate-fade-in',
      className,
    )}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

/**
 * True when a DialogDescription is rendered directly in the content tree
 * (including inside DialogHeader or fragments). Descriptions rendered by
 * a nested custom component are not visible here; such callers can pass
 * `aria-describedby` explicitly.
 */
function containsDescription(node: React.ReactNode, depth = 0): boolean {
  if (depth > 6) return false
  let found = false
  React.Children.forEach(node, (child) => {
    if (found || !React.isValidElement(child)) return
    if (child.type === DialogDescription) { found = true; return }
    const nested = (child.props as { children?: React.ReactNode })?.children
    if (nested != null && containsDescription(nested, depth + 1)) found = true
  })
  return found
}

export const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, children, onCloseAutoFocus, ...props }, ref) => {
  // Radix points aria-describedby at a description id by default and warns
  // when none is rendered. Without a description, drop the dangling
  // reference (the title still names the dialog); callers may override.
  const describedBy = 'aria-describedby' in props || containsDescription(children)
    ? {}
    : { 'aria-describedby': undefined }
  // Radix returns focus on close to a <DialogTrigger> only. A dialog opened
  // from code (a row button, a URL parameter) has none, so focus fell to the
  // document body on Escape. Remember what had focus when the dialog opened
  // and return there, unless the caller handles it or the element is gone.
  const opener = React.useRef<HTMLElement | null>(
    typeof document !== 'undefined' && document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null,
  )
  const handleCloseAutoFocus = (event: Event) => {
    onCloseAutoFocus?.(event)
    if (event.defaultPrevented) return
    const el = opener.current
    if (el && el.isConnected) {
      event.preventDefault()
      el.focus({ preventScroll: true })
    }
  }
  return (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        'fixed left-1/2 top-1/2 z-50 grid w-full max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4',
        // Bound the whole dialog to the visible viewport (dynamic units follow
        // the on-screen keyboard) and scroll inside it, so header, close button
        // and footer stay reachable on short or zoomed screens.
        'max-h-[calc(100dvh-1rem)] overflow-y-auto overscroll-contain',
        'rounded-lg border border-border bg-surface p-4 shadow-xl duration-200 sm:p-6',
        'data-[state=open]:animate-fade-in',
        className,
      )}
      {...describedBy}
      {...props}
      onCloseAutoFocus={handleCloseAutoFocus}
    >
      {children}
      <DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 hover:opacity-100 focus:outline-none">
        <X className="h-4 w-4" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
  )
})
DialogContent.displayName = DialogPrimitive.Content.displayName

export const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('flex flex-col gap-1.5 text-left', className)} {...props} />
)
// Sticks to the bottom of a scrolling dialog so Save / Cancel are always in
// reach. Negative margins cancel the dialog padding, which is re-added inside,
// so a dialog that does not overflow looks exactly as before.
export const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn('sticky bottom-0 z-10 -mx-4 -mb-4 flex flex-wrap justify-end gap-2 border-t border-border/50 bg-surface px-4 pb-4 pt-3 sm:-mx-6 sm:-mb-6 sm:px-6 sm:pb-6', className)}
    {...props}
  />
)

export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('text-lg font-semibold leading-none tracking-tight', className)}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn('text-sm text-muted', className)} {...props} />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName
