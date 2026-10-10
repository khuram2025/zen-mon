import { Link } from 'react-router-dom'
import { EyeOff, HelpCircle } from 'lucide-react'
import { fmtInt, plural } from './model'
import { hiddenCategoryNames, type CatalogFooterModel } from './softwareModel'

const link = 'rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary'

/**
 * The notes under the catalog: how many products sit in hidden categories (with a way to list them), and how many
 * titles the normaliser could not place. The numbers that have a list are links to it, and the link text carries
 * the number that list has; the installs of the hidden products have no list of their own and stay text.
 */
export function SoftwareFooter({ footer }: { footer: CatalogFooterModel }) {
  const { hidden, showingHidden, unmatched } = footer
  if (!hidden && !showingHidden && !unmatched) return null
  return (
    <ul role="list" className="space-y-1 border-t border-border/60 px-4 py-2.5 text-xs text-muted" data-software-footer="">
      {hidden && (
        <li className="flex flex-wrap items-center gap-x-1.5" data-note="hidden">
          <EyeOff className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            {plural(hidden.products, 'product')} ({plural(hidden.installs, 'installation')}) in hidden categories
            {hidden.categories.length > 0 && <> ({hiddenCategoryNames(hidden.categories)})</>} not listed.
          </span>
          <Link to={hidden.showHref} data-drill="page" data-drill-count={hidden.showTotal} className={link}>
            Show all {fmtInt(hidden.showTotal)}
          </Link>
        </li>
      )}
      {showingHidden && (
        <li className="flex flex-wrap items-center gap-x-1.5" data-note="showing-hidden">
          <EyeOff className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Including the categories hidden by default ({hiddenCategoryNames(showingHidden.categories)}).</span>
          <Link to={showingHidden.hideHref} className={link}>Hide them again</Link>
        </li>
      )}
      {unmatched && (
        <li className="flex flex-wrap items-center gap-x-1.5" data-note="unmatched">
          <HelpCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <Link to={unmatched.href} data-drill="page" data-drill-count={unmatched.count} className={link}>
            {fmtInt(unmatched.count)} {unmatched.count === 1 ? 'title' : 'titles'} unmatched
          </Link>
          <span>The normaliser could not place {unmatched.count === 1 ? 'it' : 'them'}. Catalog rules cannot be edited in this release.</span>
        </li>
      )}
    </ul>
  )
}
