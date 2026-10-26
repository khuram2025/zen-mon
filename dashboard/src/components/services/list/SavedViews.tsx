import { useEffect, useState } from 'react'
import { Bookmark, Check, Plus, Trash2 } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { VIEW_PARAM_KEYS } from './model'

export type SavedView = { id: string; name: string; query: string; created_at: string }

const MAX_VIEWS = 30

function storageKey(userId: string | undefined) {
  return `zp.services.views.v1.${userId || 'anonymous'}`
}

function readViews(key: string): SavedView[] {
  try {
    const raw = window.localStorage.getItem(key)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed)
      ? parsed.filter((v) => v && typeof v.name === 'string' && typeof v.query === 'string')
      : []
  } catch {
    return []
  }
}

function writeViews(key: string, views: SavedView[]): boolean {
  try {
    window.localStorage.setItem(key, JSON.stringify(views))
    return true
  } catch {
    return false
  }
}

/** The view-relevant part of the current URL, in a stable key order. */
export function viewQueryOf(params: URLSearchParams): string {
  const out = new URLSearchParams()
  for (const k of VIEW_PARAM_KEYS) {
    const v = params.get(k)
    if (v) out.set(k, v)
  }
  return out.toString()
}

/** Saved list views (filters, sort, page size) per user, in this browser. */
export function SavedViews({ params, onApply }: {
  params: URLSearchParams
  onApply: (query: string) => void
}) {
  const userId = useAuth((s) => s.user?.id)
  const key = storageKey(userId)
  const [views, setViews] = useState<SavedView[]>(() => readViews(key))
  const [saveOpen, setSaveOpen] = useState(false)
  const [name, setName] = useState('')

  useEffect(() => { setViews(readViews(key)) }, [key])

  const current = viewQueryOf(params)
  const activeView = views.find((v) => v.query === current)

  function persist(next: SavedView[]) {
    setViews(next)
    if (!writeViews(key, next)) toast.error('Could not save views', 'Browser storage is unavailable; views last until you leave this page.')
  }

  function save() {
    const trimmed = name.trim()
    if (!trimmed) return
    const existing = views.find((v) => v.name.toLowerCase() === trimmed.toLowerCase())
    const view: SavedView = {
      id: existing?.id || `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
      name: trimmed,
      query: current,
      created_at: new Date().toISOString(),
    }
    const next = existing
      ? views.map((v) => (v.id === existing.id ? view : v))
      : [...views, view].slice(-MAX_VIEWS)
    persist(next)
    setSaveOpen(false)
    setName('')
    toast.success(existing ? 'View updated' : 'View saved', trimmed)
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className="h-8 max-w-[180px]" title="Saved views">
            <Bookmark className="h-3.5 w-3.5" />
            <span className="truncate">{activeView ? activeView.name : 'Views'}</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent className="w-64">
          <DropdownMenuLabel>Saved views</DropdownMenuLabel>
          {views.length === 0 && (
            <div className="px-2.5 py-1.5 text-xs text-muted">
              Save the current filters, sort and page size to reuse them later. Views are stored in this browser.
            </div>
          )}
          <div className="max-h-64 overflow-y-auto">
            {views.map((v) => (
              <DropdownMenuItem key={v.id} onSelect={() => onApply(v.query)} className="group/view pr-1">
                {v.id === activeView?.id ? <Check className="text-primary" /> : <span className="w-3.5" />}
                <span className="min-w-0 flex-1 truncate" title={v.query || 'Default view'}>{v.name}</span>
                <button
                  type="button"
                  aria-label={`Delete view ${v.name}`}
                  title="Delete view"
                  className="rounded p-1 text-muted hover:bg-danger/10 hover:text-danger"
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    persist(views.filter((x) => x.id !== v.id))
                  }}
                  onPointerDown={(e) => e.stopPropagation()}
                >
                  <Trash2 />
                </button>
              </DropdownMenuItem>
            ))}
          </div>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => { setName(activeView?.name || ''); setSaveOpen(true) }}>
            <Plus /> Save current view…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Save view</DialogTitle>
            <DialogDescription>
              Saves the current filters, sort order and page size for your account in this browser.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); save() }} className="space-y-3">
            <Input
              autoFocus
              value={name}
              maxLength={60}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Production down or stale"
            />
            <p className="break-all font-mono text-xs text-muted">{current || 'Default view (no filters)'}</p>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setSaveOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={!name.trim()}>Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
