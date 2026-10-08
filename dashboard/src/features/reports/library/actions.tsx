/**
 * Template actions shared by cards, table rows and the details drawer:
 * Open / Schedule / Customize plus the overflow menu (Edit, Duplicate, Copy
 * link, Delete). The page supplies the handlers and capability flags.
 */
import { createContext, useContext } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  CalendarPlus, Copy, Info, Link2, MoreHorizontal, Pencil, Play, Trash2, Wand2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { customizeLink, editLink, openLink, type LibraryItem } from './model'

export interface LibraryActions {
  /** reports.manage (and the catalog agrees): customize, edit, duplicate, delete. */
  canManage: boolean
  /** May create schedules (manage, and not limited to a visibility scope). */
  canSchedule: boolean
  /** Why scheduling is unavailable to a manager, if it is. */
  scheduleNote: string | null
  isFavorite: (item: LibraryItem) => boolean
  toggleFavorite: (item: LibraryItem) => void
  showDetails: (item: LibraryItem) => void
  /** An Open link was followed (records it as recently viewed). */
  opened: (item: LibraryItem) => void
  schedule: (item: LibraryItem) => void
  duplicate: (item: LibraryItem) => void
  remove: (item: LibraryItem) => void
  copyLink: (item: LibraryItem) => void
  /** customId of a template with a pending duplicate. */
  busyId: string | null
}

const Ctx = createContext<LibraryActions | null>(null)
export const LibraryActionsProvider = Ctx.Provider

export function useLibraryActions(): LibraryActions {
  const v = useContext(Ctx)
  if (!v) throw new Error('useLibraryActions must be used inside LibraryActionsProvider')
  return v
}

const compactBtn = 'h-7 gap-1.5 px-2 text-xs [&_svg]:size-3.5'
const iconBtn = 'h-7 w-7 px-0 text-muted hover:text-text [&_svg]:size-4'

/**
 * Open (primary), Schedule and Customize. `iconsOnly` keeps Open labelled and
 * shows Schedule / Customize as labelled icon buttons (table rows).
 */
export function QuickActions({ item, className, compact = true, iconsOnly = false }: {
  item: LibraryItem
  className?: string
  compact?: boolean
  iconsOnly?: boolean
}) {
  const a = useLibraryActions()
  const size = compact ? compactBtn : ''
  return (
    <div className={cn('flex flex-wrap items-center gap-1', className)}>
      <Button asChild size="sm" className={size}>
        <Link to={openLink(item)} onClick={() => a.opened(item)} aria-label={`Open ${item.title}`}>
          <Play aria-hidden />
          Open
        </Link>
      </Button>
      {a.canSchedule && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className={iconsOnly ? iconBtn : size}
          onClick={() => a.schedule(item)}
          aria-label={`Schedule ${item.title}`}
          title={iconsOnly ? 'Schedule' : undefined}
        >
          <CalendarPlus aria-hidden />
          {!iconsOnly && 'Schedule'}
        </Button>
      )}
      {a.canManage && (
        <Button asChild size="sm" variant="ghost" className={iconsOnly ? iconBtn : size}>
          <Link
            to={customizeLink(item)}
            aria-label={`Customize ${item.title}`}
            title={iconsOnly ? 'Customize a copy in the designer' : 'Start the designer from a copy of this template'}
          >
            <Wand2 aria-hidden />
            {!iconsOnly && 'Customize'}
          </Link>
        </Button>
      )}
    </div>
  )
}

/** Overflow menu. `withDetails` adds "View details" (cards and rows; not the drawer itself). */
export function TemplateMenu({ item, withDetails = true, className }: { item: LibraryItem; withDetails?: boolean; className?: string }) {
  const a = useLibraryActions()
  const navigate = useNavigate()
  const custom = item.origin === 'custom'
  const edit = custom && a.canManage && item.canEdit ? editLink(item) : null
  const canDuplicate = custom && a.canManage
  const canDelete = custom && a.canManage && item.canDelete
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className={cn('h-7 w-7 text-muted hover:text-text [&_svg]:size-4', className)}
          aria-label={`More actions for ${item.title}`}
        >
          <MoreHorizontal aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        {withDetails && (
          <DropdownMenuItem onSelect={() => a.showDetails(item)}>
            <Info aria-hidden /> View details
          </DropdownMenuItem>
        )}
        {edit && (
          <DropdownMenuItem onSelect={() => navigate(edit)}>
            <Pencil aria-hidden /> Edit template
          </DropdownMenuItem>
        )}
        {canDuplicate && (
          <DropdownMenuItem disabled={a.busyId === item.customId} onSelect={() => a.duplicate(item)}>
            <Copy aria-hidden /> Duplicate
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={() => a.copyLink(item)}>
          <Link2 aria-hidden /> Copy link
        </DropdownMenuItem>
        {canDelete && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem destructive onSelect={() => a.remove(item)}>
              <Trash2 aria-hidden /> Delete…
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
