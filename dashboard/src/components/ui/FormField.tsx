import * as React from 'react'
import { cn } from '@/lib/utils'
import { Label } from './Label'

export function FormField({
  label,
  hint,
  error,
  required,
  className,
  children,
}: {
  label: string
  hint?: React.ReactNode
  error?: string | null
  required?: boolean
  className?: string
  children: React.ReactNode
}) {
  // Associate the label with a single control so it has an accessible name.
  const generated = React.useId()
  const only = React.Children.count(children) === 1 && React.isValidElement(children) ? (children as React.ReactElement<any>) : null
  const id = only ? (only.props.id ?? generated) : undefined
  const control = only && only.props.id == null ? React.cloneElement(only, { id }) : children
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id}>
        {label}
        {required && <span className="ml-0.5 text-danger">*</span>}
      </Label>
      {control}
      {hint && !error && <div className="text-[11px] text-muted">{hint}</div>}
      {error && <p className="text-[11px] text-danger">{error}</p>}
    </div>
  )
}
