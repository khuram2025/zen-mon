import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface ReportSectionProps {
  title: string
  description?: string
  icon?: ReactNode
  action?: ReactNode
  className?: string
  children: ReactNode
  /** Compact pad for tables that include their own headers/spacing. */
  padded?: boolean
}

export function ReportSection({
  title,
  description,
  icon,
  action,
  className,
  children,
  padded = true,
}: ReportSectionProps) {
  return (
    <section
      className={cn(
        'rounded-lg border border-border bg-surface shadow-card dark:shadow-card-dark',
        className,
      )}
    >
      <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          {icon && (
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
              {icon}
            </span>
          )}
          <div className="min-w-0">
            <h3 className="text-sm font-semibold tracking-tight text-text">{title}</h3>
            {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
          </div>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </header>
      <div className={padded ? 'p-5' : ''}>{children}</div>
    </section>
  )
}

export function EmptyReportState({ message = 'No data for this range yet.' }: { message?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface2 text-muted">
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={2}>
          <circle cx="12" cy="12" r="9" />
          <path d="M9 12l2 2 4-4" />
        </svg>
      </div>
      <p className="text-sm text-muted">{message}</p>
    </div>
  )
}
