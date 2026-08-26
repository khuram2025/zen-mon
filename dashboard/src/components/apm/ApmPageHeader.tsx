import type { ReactNode } from 'react'
import { KbLink, type KbArticle } from './KbLink'

interface Props {
  title: string
  description?: string
  article: KbArticle
  /** Filters/controls rendered on the right of the title row. */
  actions?: ReactNode
}

/**
 * One header shape for every APM page: title, one-line explanation of what the
 * screen answers, the page's controls, and the documentation link.
 *
 * Before this, each page hand-rolled its own header and they drifted — three
 * different heading sizes, two places for the range picker, and no way to reach
 * the docs at all.
 */
export function ApmPageHeader({ title, description, article, actions }: Props) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold tracking-tight text-text">{title}</h2>
        {description && <p className="mt-0.5 max-w-3xl text-sm text-muted">{description}</p>}
      </div>
      <div className="flex flex-shrink-0 items-center gap-2">
        {actions}
        <KbLink article={article} />
      </div>
    </div>
  )
}
