import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, ChevronRight, type LucideIcon } from 'lucide-react'

export interface NetflowPageHeaderProps {
  title: ReactNode
  icon?: LucideIcon
  description?: ReactNode
  /** Breadcrumb entries between "NetFlow" and this page, e.g. [{label: 'Collector health', to: '/netflow/health'}]. */
  parents?: { label: string; to: string }[]
  /** Breadcrumb label for this page (defaults to the title when it is text). */
  crumb?: string
  /** Right-hand controls (time picker, actions). Wraps below the title on narrow screens. */
  actions?: ReactNode
  /**
   * Target of the root "NetFlow" crumb (default `/netflow`). Pages inside an investigation pass the
   * overview link with their filter and window so going back keeps the context.
   */
  rootTo?: string
}

/** The breadcrumb + title block shared by NetFlow pages. */
export function NetflowPageHeader({ title, icon: Icon, description, parents = [], crumb, actions, rootTo = '/netflow' }: NetflowPageHeaderProps) {
  const here = crumb ?? (typeof title === 'string' ? title : null)
  return (
    <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
      <div className="min-w-0">
        <nav aria-label="Breadcrumb" className="mb-1 flex flex-wrap items-center gap-1.5 text-xs text-text2">
          <Link to={rootTo} className="inline-flex items-center gap-1 hover:text-text">
            <ArrowLeft className="h-3 w-3" aria-hidden />
            NetFlow
          </Link>
          {parents.map((p) => (
            <span key={p.to} className="inline-flex items-center gap-1.5">
              <ChevronRight className="h-3 w-3" aria-hidden />
              <Link to={p.to} className="hover:text-text">{p.label}</Link>
            </span>
          ))}
          {here && (
            <span className="inline-flex items-center gap-1.5">
              <ChevronRight className="h-3 w-3" aria-hidden />
              <span aria-current="page">{here}</span>
            </span>
          )}
        </nav>
        <h1 className="flex items-center gap-2 break-words text-xl font-semibold tracking-tight text-text">
          {Icon && <Icon className="h-5 w-5 shrink-0 text-primary" aria-hidden />}
          {title}
        </h1>
        {description && <p className="mt-0.5 max-w-3xl text-xs text-text2">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
