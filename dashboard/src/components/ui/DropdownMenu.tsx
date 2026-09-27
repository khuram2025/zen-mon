import * as React from 'react'
import * as Menu from '@radix-ui/react-dropdown-menu'
import { cn } from '@/lib/utils'

export const DropdownMenu = Menu.Root
export const DropdownMenuTrigger = Menu.Trigger

export const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof Menu.Content>,
  React.ComponentPropsWithoutRef<typeof Menu.Content>
>(({ className, sideOffset = 6, align = 'end', ...props }, ref) => (
  <Menu.Portal>
    <Menu.Content
      ref={ref}
      sideOffset={sideOffset}
      align={align}
      className={cn(
        'z-50 min-w-[200px] overflow-hidden rounded-lg border border-border bg-surface p-1 text-text shadow-elevated',
        className,
      )}
      {...props}
    />
  </Menu.Portal>
))
DropdownMenuContent.displayName = 'DropdownMenuContent'

export const DropdownMenuItem = React.forwardRef<
  React.ElementRef<typeof Menu.Item>,
  React.ComponentPropsWithoutRef<typeof Menu.Item> & { destructive?: boolean }
>(({ className, destructive, ...props }, ref) => (
  <Menu.Item
    ref={ref}
    className={cn(
      'flex cursor-pointer select-none items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] outline-none',
      'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      destructive
        ? 'text-danger data-[highlighted]:bg-danger/10'
        : 'data-[highlighted]:bg-surface2',
      '[&_svg]:h-3.5 [&_svg]:w-3.5 [&_svg]:shrink-0',
      className,
    )}
    {...props}
  />
))
DropdownMenuItem.displayName = 'DropdownMenuItem'

export function DropdownMenuSeparator({ className }: { className?: string }) {
  return <Menu.Separator className={cn('-mx-1 my-1 h-px bg-border', className)} />
}

export function DropdownMenuLabel({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <Menu.Label className={cn('px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide text-muted', className)}>
      {children}
    </Menu.Label>
  )
}
