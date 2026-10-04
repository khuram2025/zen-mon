import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Copy, Layers, Lock, Pencil, Pin, PinOff, Play, Search, Trash2, Users } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { FilterChips } from '@/components/netflow/FilterChips'
import { SaveViewDialog, type SaveViewDraft } from '@/components/netflow/SaveViewDialog'
import { cn, relativeTime } from '@/lib/utils'
import { buildSavedQuery, isFlowFilterEmpty, type SavedQueryWindow, type SavedViewKind } from '@/lib/netflow/filters'
import { formatDateTime } from '@/lib/netflow/format'
import { displayTimeZone, savedQueryWindow, type NfWindow } from '@/lib/netflow/window'
import { serverNow } from '@/lib/serverClock'
import { errorText, useSavedViewMutations, useSavedViews } from './forensics/api'
import {
  groupSavedViews,
  KIND_LABELS,
  pageSettingsSummary,
  savedViewLink,
  savedViewQuery,
  savedWindowLabel,
  sortLabel,
  type SavedView,
} from './forensics/model'

const RANGE_HOURS: Record<string, number> = { '1h': 1, '24h': 24, '7d': 168, '1M': 720 }

/** An NfWindow for the save dialog from a stored window (relative keeps its preset). */
function windowForDialog(w: SavedQueryWindow, tz: string): NfWindow {
  const now = serverNow()
  if (w.mode === 'relative') {
    const hours = RANGE_HOURS[w.range] ?? null
    const from = new Date(now - (hours ?? 24) * 3_600_000).toISOString()
    return {
      key: w.range,
      mode: hours !== null ? 'relative' : 'fixed',
      hours,
      from,
      to: new Date(now).toISOString(),
      tz,
      label: savedWindowLabel(w, tz),
    }
  }
  return { key: 'custom', mode: 'fixed', hours: null, from: w.from, to: w.to, tz, label: savedWindowLabel(w, tz) }
}

type KindFilter = SavedViewKind | 'all'

/**
 * Saved Views (NF-05, NF-12, NF-34, FE-10): private and team views with their owner, search,
 * Pinned / Mine / Shared groups, and edit, duplicate, pin and delete with visible progress and
 * errors. Buttons follow the server's `can_edit` / `can_delete`; the API enforces them either way.
 */
export function NetflowSavedViewsPage() {
  const tz = displayTimeZone()
  const views = useSavedViews()
  const { update, duplicate, remove } = useSavedViewMutations()
  const [q, setQ] = useState('')
  const [kind, setKind] = useState<KindFilter>('all')
  const [editing, setEditing] = useState<SavedView | null>(null)
  const [deleting, setDeleting] = useState<SavedView | null>(null)
  const list = views.data?.data ?? []
  const groups = useMemo(() => groupSavedViews(list, q, kind), [list, q, kind])
  const shown = groups.pinned.length + groups.mine.length + groups.shared.length + groups.othersPrivate.length
  const truncated = views.data?.meta?.truncated?.more === true

  const togglePin = (v: SavedView) =>
    update.mutate(
      { id: v.id, patch: { pinned: !v.pinned } },
      {
        onSuccess: (r) => toast.success(r.pinned ? 'Pinned' : 'Unpinned', `"${r.name}"`),
        onError: (err) => toast.error('The view was not changed', errorText(err)),
      },
    )

  const dup = (v: SavedView) =>
    duplicate.mutate(v.id, {
      onSuccess: (r) => toast.success('Private copy created', `"${r.name}" is yours to change.`),
      onError: (err) => toast.error('The view was not duplicated', errorText(err)),
    })

  const saveEdit = (d: SaveViewDraft) => {
    if (!editing) return
    const { query, path } = savedViewQuery(editing)
    const patch: Parameters<typeof update.mutate>[0]['patch'] = {
      name: d.name,
      description: d.description || null,
      pinned: d.pinned,
    }
    if (d.visibility !== editing.visibility) patch.visibility = d.visibility
    if (d.timeMode !== query.window.mode) {
      const w = windowForDialog(query.window, tz)
      patch.query = {
        ...buildSavedQuery({ ...query, window: savedQueryWindow(w, d.timeMode) }),
        ...(path ? { path } : {}),
      }
    }
    update.mutate(
      { id: editing.id, patch },
      {
        onSuccess: (r) => {
          setEditing(null)
          toast.success('Saved view updated', `"${r.name}"`)
        },
      },
    )
  }

  const editingQuery = editing ? savedViewQuery(editing) : null

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Saved Views"
        icon={Layers}
        description={
          <>
            Reusable NetFlow searches and dashboards. <strong className="font-semibold text-text">Private</strong> views are
            visible only to you (and to administrators); <strong className="font-semibold text-text">shared</strong> views are
            visible to everyone with NetFlow access, and each viewer still sees only the exporters they may see. Only the owner
            can change a private view; changing or deleting a shared view needs the Manage shared views permission.
          </>
        }
      />

      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-text2" aria-hidden />
              <label htmlFor="nf-sv-search" className="sr-only">Search saved views</label>
              <Input
                id="nf-sv-search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search by name, description or owner"
                className="pl-8"
              />
            </div>
            <div role="group" aria-label="View type" className="inline-flex flex-wrap gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5">
              {(['all', 'forensics', 'overview', 'section'] as KindFilter[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  aria-pressed={kind === k}
                  onClick={() => setKind(k)}
                  className={cn(
                    'rounded px-2 py-1 text-[11px] font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
                    kind === k ? 'bg-primary text-on-primary' : 'text-text2 hover:text-text',
                  )}
                >
                  {k === 'all' ? 'All' : KIND_LABELS[k]}
                </button>
              ))}
            </div>
          </div>

          <QueryPanel
            query={views}
            label="saved views"
            skeleton="rows"
            isEmpty={() => list.length === 0}
            empty={
              <span>
                No saved views yet. Run a search in{' '}
                <Link to="/netflow/forensics" className="text-primary underline-offset-2 hover:underline">Forensics</Link>{' '}
                or open the overview, then choose <span className="font-semibold">Save view</span>.
              </span>
            }
          >
            {() => (
              <div className="space-y-5">
                {truncated && (
                  <p className="text-[11px] text-text2">Showing the first {list.length} views; search to narrow the list.</p>
                )}
                {shown === 0 && <p className="py-6 text-center text-xs text-text2">No saved views match “{q}”.</p>}
                <Group title="Pinned" views={groups.pinned} tz={tz} onEdit={setEditing} onDuplicate={dup} onPin={togglePin} onDelete={setDeleting} busy={update.isPending || duplicate.isPending} />
                <Group title="My views" views={groups.mine} tz={tz} onEdit={setEditing} onDuplicate={dup} onPin={togglePin} onDelete={setDeleting} busy={update.isPending || duplicate.isPending} />
                <Group title="Shared with the team" views={groups.shared} tz={tz} onEdit={setEditing} onDuplicate={dup} onPin={togglePin} onDelete={setDeleting} busy={update.isPending || duplicate.isPending} />
                <Group
                  title="Other users' private views"
                  note="Visible to you because you are an administrator; their owners see them as private."
                  views={groups.othersPrivate}
                  tz={tz}
                  onEdit={setEditing}
                  onDuplicate={dup}
                  onPin={togglePin}
                  onDelete={setDeleting}
                  busy={update.isPending || duplicate.isPending}
                />
              </div>
            )}
          </QueryPanel>
        </CardContent>
      </Card>

      {editing && editingQuery && (
        <SaveViewDialog
          open
          mode="edit"
          onOpenChange={(o) => {
            if (!o) {
              setEditing(null)
              update.reset()
            }
          }}
          initial={{
            name: editing.name,
            description: editing.description ?? '',
            visibility: editing.visibility,
            pinned: editing.pinned,
            timeMode: editingQuery.query.window.mode,
          }}
          filter={editingQuery.query.filter}
          window={windowForDialog(editingQuery.query.window, tz)}
          canShare={editing.can_share && (editing.can_change_visibility || editing.visibility === 'shared')}
          pending={update.isPending}
          error={update.isError ? errorText(update.error) : null}
          extraSummary={
            <>
              {KIND_LABELS[editing.kind]} view{editingQuery.path ? ` of ${editingQuery.path}` : ''}.{' '}
              {sortLabel(editingQuery.query) ?? ''}
              {(() => {
                const settings = pageSettingsSummary(editingQuery.query.params, editing.kind === 'section' ? editingQuery.query.sort : null)
                return settings ? ` ${settings}.` : ''
              })()}
              {!editing.can_change_visibility && ' Only the owner can change who can see it.'}
            </>
          }
          onSubmit={saveEdit}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => {
          if (!o && !remove.isPending) setDeleting(null)
        }}
        title="Delete saved view?"
        destructive
        confirmText={remove.isPending ? 'Deleting…' : 'Delete'}
        loading={remove.isPending}
        description={
          deleting ? (
            <>
              “{deleting.name}” will be removed for {deleting.visibility === 'shared' ? 'everyone it is shared with' : 'you'}. This
              cannot be undone.
            </>
          ) : null
        }
        onConfirm={() => {
          if (!deleting) return
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success('Saved view deleted', `“${deleting.name}”`)
              setDeleting(null)
            },
            onError: (err) => toast.error('The view was not deleted', errorText(err)),
          })
        }}
      />
    </div>
  )
}

interface GroupProps {
  title: string
  note?: string
  views: SavedView[]
  tz: string
  busy: boolean
  onEdit: (v: SavedView) => void
  onDuplicate: (v: SavedView) => void
  onPin: (v: SavedView) => void
  onDelete: (v: SavedView) => void
}

function Group({ title, note, views, ...rest }: GroupProps) {
  if (!views.length) return null
  return (
    <section aria-label={title} className="space-y-2">
      <h2 className="text-[11px] font-semibold uppercase tracking-wide text-text2">
        {title} <span className="font-normal">({views.length})</span>
      </h2>
      {note && <p className="text-[11px] text-text2">{note}</p>}
      <ul role="list" className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3">
        {views.map((v) => (
          <li key={v.id}>
            <ViewCard view={v} {...rest} />
          </li>
        ))}
      </ul>
    </section>
  )
}

function IconAction({ label, onClick, disabled, children, tone }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode; tone?: 'danger' }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        'rounded-md p-1.5 text-text2 hover:bg-surface3 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-50',
        tone === 'danger' && 'hover:bg-danger/10 hover:text-danger-fg',
      )}
    >
      {children}
    </button>
  )
}

function ViewCard({ view: v, tz, busy, onEdit, onDuplicate, onPin, onDelete }: { view: SavedView } & Omit<GroupProps, 'title' | 'note' | 'views'>) {
  const { query, path, legacyTime } = savedViewQuery(v)
  const owner = v.is_owner ? 'You' : v.owner_username ?? 'a deleted user'
  const sort = sortLabel(query)
  const settings = pageSettingsSummary(query.params, v.kind === 'section' ? query.sort : null)
  return (
    <article className="flex h-full flex-col gap-2 rounded-md border border-border bg-surface2/40 p-3" aria-label={v.name}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 break-words text-sm font-semibold text-text">
            {v.pinned && <Pin className="h-3.5 w-3.5 shrink-0 text-warning-fg" aria-label="Pinned" />}
            {v.name}
          </h3>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-text2">
            <span className="inline-flex items-center gap-1 rounded border border-border bg-surface px-1.5 py-px font-medium text-text">
              {v.visibility === 'shared' ? <Users className="h-3 w-3" aria-hidden /> : <Lock className="h-3 w-3" aria-hidden />}
              {v.visibility === 'shared' ? 'Shared' : 'Private'}
            </span>
            <span className="rounded border border-border bg-surface px-1.5 py-px">{KIND_LABELS[v.kind]}</span>
            <span>Owner: {owner}</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center">
          {v.can_edit && (
            <>
              <IconAction label={v.pinned ? `Unpin ${v.name}` : `Pin ${v.name}`} onClick={() => onPin(v)} disabled={busy}>
                {v.pinned ? <PinOff className="h-3.5 w-3.5" aria-hidden /> : <Pin className="h-3.5 w-3.5" aria-hidden />}
              </IconAction>
              <IconAction label={`Edit ${v.name}`} onClick={() => onEdit(v)}>
                <Pencil className="h-3.5 w-3.5" aria-hidden />
              </IconAction>
            </>
          )}
          <IconAction label={`Duplicate ${v.name} as a private copy`} onClick={() => onDuplicate(v)} disabled={busy}>
            <Copy className="h-3.5 w-3.5" aria-hidden />
          </IconAction>
          {v.can_delete && (
            <IconAction label={`Delete ${v.name}`} onClick={() => onDelete(v)} tone="danger">
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </IconAction>
          )}
        </div>
      </div>
      {v.description && <p className="text-[12px] text-text">{v.description}</p>}
      <div className="space-y-1">
        {isFlowFilterEmpty(query.filter) ? (
          <p className="text-[11px] text-text2">No filters: all traffic in the period.</p>
        ) : (
          <FilterChips filter={query.filter} tz={tz} />
        )}
        <p className="text-[11px] text-text2">
          {savedWindowLabel(query.window, tz)}
          {sort ? ` · ${sort}` : ''}
          {settings ? ` · ${settings}` : ''}
          {query.columns?.length ? ` · ${query.columns.length} columns` : ''}
          {path ? ` · ${path}` : ''}
        </p>
      </div>
      {(legacyTime || v.query_issues.length > 0) && (
        <p className="flex items-start gap-1 text-[11px] text-text">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning-fg" aria-hidden />
          <span>
            {legacyTime && 'Saved before periods were stored: it opens with the last hour. '}
            {v.query_issues.length > 0 &&
              `Not restored: ${v.query_issues.map((i) => `${i.key} “${i.value}” (${i.reason})`).join('; ')}.`}
          </span>
        </p>
      )}
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1 text-[11px] text-text2">
        <span title={v.updated_at ? formatDateTime(v.updated_at, tz) : undefined}>
          Updated {relativeTime(v.updated_at)}
          {v.updated_by_username && !v.is_owner && v.updated_by_username !== v.owner_username ? ` by ${v.updated_by_username}` : ''}
        </span>
        <Button asChild size="sm" variant="outline" className="h-7 gap-1 px-2 text-[11px]">
          <Link to={savedViewLink(v)} aria-label={`Open ${v.name}`}>
            <Play className="h-3 w-3" aria-hidden />
            Open
          </Link>
        </Button>
      </div>
    </article>
  )
}
