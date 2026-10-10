import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { OS_KIND_OPTIONS, withOsKind, type OsKindChoice } from './osTreeModel'

/**
 * The All / Servers / Network segmented control (`?kind=`). Each segment is a link to the same page with the other
 * kind, so a middle click, Back and a copied address all work; the current one is marked with `aria-current` and a
 * fill (never colour alone: it is also the only one with a filled background and a heavier weight).
 */
export function OsKindSwitch({ kind, params, label = 'Show' }: { kind: OsKindChoice; params: URLSearchParams; label?: string }) {
  const { pathname } = useLocation()
  return (
    <div role="group" aria-label={label} className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5" data-kind-switch="">
      {OS_KIND_OPTIONS.map((o) => {
        const active = o.value === kind
        const qs = withOsKind(params, o.value).toString()
        return (
          <Link
            key={o.value}
            to={qs ? `${pathname}?${qs}` : pathname}
            replace
            aria-current={active ? 'true' : undefined}
            className={cn(
              'rounded px-3 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary',
              active ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text',
            )}
          >
            {o.label}
          </Link>
        )
      })}
    </div>
  )
}
