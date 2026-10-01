import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Bookmark } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { SaveViewDialog, type SaveViewDraft } from '@/components/netflow/SaveViewDialog'
import { useCan } from '@/stores/auth'
import { buildSavedQuery, type FlowFilter, type SavedViewKind } from '@/lib/netflow/filters'
import { savedQueryWindow, type NfMetaWindow, type NfWindow } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import { errorText, useSavedViewMutations } from './forensics/api'

export interface SaveCurrentViewButtonProps {
  /** 'overview' for the overview and exporter pages, 'section' for breakdown pages. */
  kind: SavedViewKind
  /** The applied page filter (from the URL). */
  filter: FlowFilter
  /** The page window. */
  window: NfWindow
  /** `meta.window` of the data on screen: "this exact period" pins exactly those bounds. */
  metaWindow?: Pick<NfMetaWindow, 'from' | 'to'> | null
  /** Page the view reopens; defaults to the current path (e.g. /netflow/top-talkers, /netflow/devices/…). */
  path?: string
  sort?: string | null
  order?: 'asc' | 'desc' | null
  columns?: string[] | null
  /**
   * Page settings that change what the page ranks or shows (`by`, `side`, `resolve`, `col`, `dir`):
   * saved with the view so it reopens with the same meaning (FE-10). Empty values are left out.
   */
  params?: Record<string, string | null | undefined> | null
  /** Plain-language list of the saved page settings for the dialog, e.g. "Ranked by packets received". */
  settingsSummary?: string | null
  /** Button text (default "Save view"). */
  label?: string
  /** Icon-only button (still named for assistive technology). */
  compact?: boolean
  className?: string
}

/**
 * "Save current view" for overview and breakdown pages (FE-10): the applied filter, the window as
 * relative or exact, and the page it reopens, saved as a governed v2 saved view. Page owners place
 * it next to their time picker.
 */
export function SaveCurrentViewButton({
  kind,
  filter,
  window: w,
  metaWindow,
  path,
  sort,
  order,
  columns,
  params,
  settingsSummary,
  label = 'Save view',
  compact,
  className,
}: SaveCurrentViewButtonProps) {
  const [open, setOpen] = useState(false)
  const location = useLocation()
  const can = useCan()
  const { create } = useSavedViewMutations()
  const target = path ?? location.pathname

  const submit = (d: SaveViewDraft) => {
    const query = {
      ...buildSavedQuery({ kind, filter, window: savedQueryWindow(w, d.timeMode, metaWindow), sort, order, columns, params }),
      ...(target && target.startsWith('/netflow') && kind !== 'forensics' ? { path: target.split('?')[0] } : {}),
    }
    create.mutate(
      { name: d.name, description: d.description || null, visibility: d.visibility, pinned: d.pinned, kind, query },
      {
        onSuccess: (view) => {
          setOpen(false)
          toast.success('View saved', `"${view.name}" is in Saved Views${view.visibility === 'shared' ? ', shared with the team' : ''}.`)
        },
      },
    )
  }

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className={cn('gap-1.5', className)}
        onClick={() => {
          create.reset()
          setOpen(true)
        }}
        disabled={!can('netflow.view')}
        aria-label={compact ? label : undefined}
        title={compact ? label : undefined}
      >
        <Bookmark aria-hidden />
        {!compact && label}
      </Button>
      <SaveViewDialog
        open={open}
        onOpenChange={setOpen}
        filter={filter}
        window={w}
        metaWindow={metaWindow}
        pending={create.isPending}
        error={create.isError ? errorText(create.error) : null}
        extraSummary={
          <>
            {settingsSummary ? `${settingsSummary}. ` : ''}Opens {target}.{' '}
            <Link to="/netflow/saved-views" className="text-primary underline-offset-2 hover:underline">
              Manage saved views
            </Link>
          </>
        }
        onSubmit={submit}
      />
    </>
  )
}

export default SaveCurrentViewButton
