import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, Layers, Pin, Trash2, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'

type SavedView = {
  id: string
  name: string
  description: string | null
  query: Record<string, any>
  pinned: boolean
  created_at: string
  updated_at: string
}

export function NetflowSavedViewsPage() {
  const qc = useQueryClient()
  const views = useQuery<SavedView[]>({
    queryKey: ['netflow', 'saved-views'],
    queryFn: async () => (await api.get('/netflow/saved-views')).data,
  })
  const del = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/netflow/saved-views/${id}`)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['netflow', 'saved-views'] }),
  })

  const buildLink = (q: Record<string, any>) => {
    const p = new URLSearchParams()
    for (const k of ['src', 'dst', 'proto', 'src_port', 'dst_port', 'min_bytes', 'dscp', 'exporter']) {
      if (q[k]) p.set(k, q[k])
    }
    return `/netflow/forensics${p.toString() ? `?${p.toString()}` : ''}`
  }

  return (
    <div className="space-y-4">
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
          <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
            <ArrowLeft className="h-3 w-3" />
            NetFlow
          </Link>
          <ChevronRight className="h-3 w-3" />
          <span>Saved Views</span>
        </div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Layers className="h-5 w-5 text-primary" />
          Saved Views
        </h1>
        <p className="text-xs text-muted">Reusable forensic queries shared across your team.</p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">{views.data?.length ?? 0} saved views</CardTitle>
        </CardHeader>
        <CardContent>
          {(views.data?.length ?? 0) === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-surface2/30 p-10 text-center text-xs text-muted">
              No saved views yet. Open <Link to="/netflow/forensics" className="text-primary hover:underline">Forensics</Link>, configure a query, and click <span className="text-text">Save view</span>.
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {(views.data || []).map((v) => (
                <div key={v.id} className="flex flex-col gap-2 rounded-md border border-border bg-surface2/40 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      {v.pinned && <Pin className="h-3.5 w-3.5 shrink-0 text-amber-400" />}
                      <span className="truncate text-sm font-semibold">{v.name}</span>
                    </div>
                    <button
                      onClick={() => { if (window.confirm(`Delete "${v.name}"?`)) del.mutate(v.id) }}
                      className="rounded p-1 text-muted hover:bg-rose-500/10 hover:text-rose-400"
                      title="Delete"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {v.description && <p className="text-[11px] text-muted">{v.description}</p>}
                  <div className="flex flex-wrap gap-1 text-[10px]">
                    {Object.entries(v.query).filter(([_, val]) => val).slice(0, 8).map(([k, val]) => (
                      <span key={k} className="rounded-full border border-border bg-surface px-1.5 py-0.5 text-muted">
                        <span className="text-text">{k}</span>: {String(val)}
                      </span>
                    ))}
                  </div>
                  <div className="mt-auto flex items-center justify-between gap-2 pt-2 text-[10px] text-muted">
                    <span>Updated {relativeTime(v.updated_at)}</span>
                    <Link
                      to={buildLink(v.query)}
                      className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-2 py-1 text-primary hover:bg-primary/20"
                    >
                      <Search className="h-3 w-3" />
                      Run
                    </Link>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
