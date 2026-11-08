import { useMemo } from 'react'
import { EyeOff, Tags } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NAV_GROUPS } from '@/components/layout/navigation'
import { canFromPermissions, filterNavGroups, firstAllowedPath, type PageSet } from '@/components/layout/navAccess'
import { TagBadge } from '@/components/tags/TagBadge'
import { tagColorMap, useTags } from '@/hooks/useTags'

/** The sidebar a role's members will get: the same filter the real sidebar
 *  applies, run over the permissions (and page list) being edited. */
export function MenuPreview({ permissions, pages = null, compact, className }: { permissions: Iterable<string>; pages?: PageSet; compact?: boolean; className?: string }) {
  const perms = useMemo(() => [...permissions], [permissions])
  const groups = useMemo(() => filterNavGroups(NAV_GROUPS, canFromPermissions(perms), pages), [perms, pages])
  const landing = useMemo(() => (pages ? firstAllowedPath(NAV_GROUPS, canFromPermissions(perms), pages) : null), [perms, pages])
  const hidden = NAV_GROUPS.filter((g) => !groups.some((x) => x.id === g.id))

  return (
    <div className={cn('space-y-2', className)}>
      {groups.length === 0 ? (
        <p className="flex items-start gap-1.5 rounded-md border border-dashed border-border px-2.5 py-2 text-xs text-muted">
          <EyeOff className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          No menu entries yet. Members could sign in but would only see their profile.
        </p>
      ) : (
        <ul className="space-y-1.5" aria-label="Menu sections members will see">
          {groups.map((g) => (
            <li key={g.id}>
              <div className="flex items-center gap-1.5 text-xs font-semibold text-text">
                <g.icon className="h-3.5 w-3.5 text-primary" aria-hidden />
                <span className="truncate">{g.label}</span>
              </div>
              {!compact && (
                <div className="ml-5 mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-muted">
                  {g.items.map((i) => <span key={i.to}>{i.label}{pages && i.children && i.children.length > 1 ? ` (${i.children.length})` : ''}</span>)}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {landing && !compact && (
        <p className="text-xs text-muted">Members land on: <span className="font-medium text-text">{landingLabel(landing)}</span></p>
      )}
      {hidden.length > 0 && groups.length > 0 && (
        <p className="text-xs text-muted">
          Hidden: {hidden.map((g) => g.label).join(', ')}
        </p>
      )}
    </div>
  )
}

function landingLabel(to: string): string {
  for (const g of NAV_GROUPS) for (const i of g.items) {
    if (i.to === to && !i.children) return `${g.label} › ${i.label}`
    for (const c of i.children || []) if (c.to === to) return `${g.label} › ${i.label} › ${c.label}`
  }
  return to
}

/** Scope tags as coloured chips, or a muted "All resources" label. */
export function ScopeChips({ tags, empty = 'All resources', className, max = 6 }: { tags?: string[] | null; empty?: string; className?: string; max?: number }) {
  const { data: defs } = useTags()
  const colors = useMemo(() => tagColorMap(defs), [defs])
  const list = tags || []
  if (list.length === 0) return <span className={cn('text-xs text-muted', className)}>{empty}</span>
  return (
    <span className={cn('inline-flex flex-wrap items-center gap-1', className)}>
      <Tags className="h-3 w-3 text-muted" aria-label="Scope tags" />
      {list.slice(0, max).map((t) => <TagBadge key={t} name={t} color={colors[t.toLowerCase()]} />)}
      {list.length > max && <span className="text-xs text-muted">+{list.length - max}</span>}
    </span>
  )
}
