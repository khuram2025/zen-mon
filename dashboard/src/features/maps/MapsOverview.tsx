import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Bell, Copy, ExternalLink, Map as MapIcon, MonitorPlay, Network, Pencil, Radio, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { useBreadcrumbDetail } from '@/components/layout/breadcrumbDetail'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { StatusBar } from './canvas/NocOverlays'
import { STATUS_HEX, formatAgo, type ManualMapListItem } from './core'

type Sorted = ManualMapListItem & { down: number; warn: number; total: number; crit: number; active: number }

function rank(m: Sorted) {
  return m.down * 1000 + m.crit * 100 + m.warn * 10 + Math.min(9, m.active)
}

/* /maps — every network map at a glance: health bar, device states, active
 * alerts, and one click to edit, watch live or put it on the NOC wall. Maps
 * with problems sort first. The wall rotation builder makes a URL a wall PC
 * can open on its own (it cycles through the chosen maps). */
export function MapsOverview() {
  const navigate = useNavigate()
  useBreadcrumbDetail('All maps')
  const [q, setQ] = useState('')
  const [rotationOpen, setRotationOpen] = useState(false)
  const mapsQ = useQuery<{ data: ManualMapListItem[] }>({
    queryKey: ['manual-maps'],
    queryFn: async () => (await api.get('/maps')).data,
    refetchInterval: 30_000,
  })
  const maps = useMemo<Sorted[]>(() => (mapsQ.data?.data || []).map((m) => {
    const sc = m.status_counts || {}
    return {
      ...m,
      down: sc.down || 0,
      warn: sc.degraded || 0,
      total: Object.values(sc).reduce((a, b) => a + (b || 0), 0),
      crit: m.alerts?.critical || 0,
      active: m.alerts?.active || 0,
    }
  }).sort((a, b) => rank(b) - rank(a) || a.name.localeCompare(b.name)), [mapsQ.data])
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase()
    return t ? maps.filter((m) => m.name.toLowerCase().includes(t) || (m.description || '').toLowerCase().includes(t)) : maps
  }, [maps, q])
  const totals = useMemo(() => maps.reduce((acc, m) => ({
    nodes: acc.nodes + m.total, down: acc.down + m.down, warn: acc.warn + m.warn, crit: acc.crit + m.crit, impacted: acc.impacted + (m.down || m.warn ? 1 : 0),
  }), { nodes: 0, down: 0, warn: 0, crit: 0, impacted: 0 }), [maps])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text">Network maps</h1>
          <p className="text-xs text-muted">Every map with its live health. Maps with problems come first.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a map…" className="h-8 w-52 rounded-md border border-border bg-surface pl-7 pr-2 text-xs text-text outline-none focus:border-primary/60" />
          </div>
          <Button variant="outline" size="sm" onClick={() => setRotationOpen(true)} disabled={!maps.length}><MonitorPlay className="mr-1 h-3.5 w-3.5" /> NOC wall rotation…</Button>
          <Button size="sm" onClick={() => navigate('/maps/manual')}><Pencil className="mr-1 h-3.5 w-3.5" /> Open editor</Button>
        </div>
      </div>

      {/* Estate summary */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Summary label="Maps" value={maps.length} />
        <Summary label="Nodes on maps" value={totals.nodes} />
        <Summary label="Maps impacted" value={totals.impacted} tone={totals.impacted ? 'warning' : 'success'} />
        <Summary label="Nodes down" value={totals.down} tone={totals.down ? 'danger' : 'success'} />
        <Summary label="Critical alerts" value={totals.crit} tone={totals.crit ? 'danger' : 'success'} />
      </div>

      {mapsQ.isLoading ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-44 animate-pulse rounded-xl border border-border bg-surface" />)}
        </div>
      ) : shown.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-16 text-center">
          <MapIcon className="h-8 w-8 text-muted" />
          <div className="text-sm font-semibold text-text">{maps.length ? 'No map matches' : 'No maps yet'}</div>
          {!maps.length && <Button size="sm" onClick={() => navigate('/maps/manual')}>Create your first map</Button>}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {shown.map((m) => <MapCard key={m.id} m={m} />)}
        </div>
      )}

      {rotationOpen && <RotationDialog maps={maps} onClose={() => setRotationOpen(false)} />}
    </div>
  )
}

function Summary({ label, value, tone }: { label: string; value: number; tone?: 'danger' | 'warning' | 'success' }) {
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-2 shadow-card dark:shadow-card-dark">
      <div className="text-2xs font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('font-mono text-xl font-bold', tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : tone === 'success' ? 'text-success' : 'text-text')}>{value}</div>
    </div>
  )
}

function MapCard({ m }: { m: Sorted }) {
  const tone = m.down ? STATUS_HEX.down : m.warn ? STATUS_HEX.degraded : m.total ? STATUS_HEX.up : STATUS_HEX.unknown
  const href = `/maps/manual?map=${m.id}`
  return (
    <div className="group flex flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-card transition hover:border-primary/40 dark:shadow-card-dark">
      <div className="h-1" style={{ background: tone }} />
      <Link to={`${href}&mode=live`} className="flex flex-1 flex-col gap-2 p-3">
        <div className="flex items-start gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg" style={{ background: `${tone}22`, color: tone }}>
            {m.down ? <AlertTriangle className="h-4 w-4" /> : <Network className="h-4 w-4" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-text" title={m.name}>{m.name}</div>
            <div className="truncate text-2xs text-muted">{m.description || `${m.link_count} links`}</div>
          </div>
          {m.active > 0 && (
            <span className={cn('flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-0.5 font-mono text-2xs font-bold text-white', m.crit ? 'bg-danger' : 'bg-warning')} title={`${m.active} active alerts${m.crit ? ` · ${m.crit} critical` : ''}`}>
              <Bell className="h-3 w-3" />{m.active}
            </span>
          )}
        </div>
        <StatusBar counts={m.status_counts || {}} total={m.total} className="h-2" />
        <div className="grid grid-cols-4 gap-1 text-center">
          <Stat n={m.down} label="Down" cls={m.down ? 'text-danger' : 'text-muted'} />
          <Stat n={m.warn} label="Warning" cls={m.warn ? 'text-warning' : 'text-muted'} />
          <Stat n={m.status_counts?.up || 0} label="Up" cls="text-success" />
          <Stat n={m.total} label="Nodes" cls="text-text" />
        </div>
      </Link>
      <div className="flex items-center gap-1 border-t border-border px-2 py-1.5">
        <span className="flex-1 truncate pl-1 text-2xs text-muted">updated {formatAgo(m.updated_at)}</span>
        <Link to={href} className="flex items-center gap-1 rounded px-1.5 py-1 text-2xs font-semibold text-text2 hover:bg-primary/10 hover:text-text" title="Edit the map"><Pencil className="h-3 w-3" /> Edit</Link>
        <Link to={`${href}&mode=live`} className="flex items-center gap-1 rounded px-1.5 py-1 text-2xs font-semibold text-text2 hover:bg-primary/10 hover:text-text" title="Live view"><Radio className="h-3 w-3" /> Live</Link>
        <a href={`${href}&mode=live&wall=1`} target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded px-1.5 py-1 text-2xs font-semibold text-text2 hover:bg-primary/10 hover:text-text" title="Open on a NOC wall (new tab)"><MonitorPlay className="h-3 w-3" /> Wall</a>
      </div>
    </div>
  )
}

function Stat({ n, label, cls }: { n: number; label: string; cls: string }) {
  return (
    <div className="rounded-md bg-surface2/50 py-1">
      <div className={cn('font-mono text-base font-bold leading-tight', cls)}>{n}</div>
      <div className="text-2xs text-muted">{label}</div>
    </div>
  )
}

const INTERVALS = [30, 60, 120, 300]

function RotationDialog({ maps, onClose }: { maps: Sorted[]; onClose: () => void }) {
  const [picked, setPicked] = useState<string[]>(() => maps.slice(0, Math.min(4, maps.length)).map((m) => m.id))
  const [sec, setSec] = useState(60)
  const [attention, setAttention] = useState(true)
  const url = picked.length
    ? `${window.location.origin}/maps/manual?map=${picked[0]}&mode=live&wall=1&kiosk=1${picked.length > 1 ? `&maps=${picked.join(',')}&rotate=${sec}` : ''}${attention ? '' : '&attention=0'}`
    : ''
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle className="flex items-center gap-2"><MonitorPlay className="h-4 w-4 text-primary" /> NOC wall rotation</DialogTitle></DialogHeader>
        <DialogDescription>Pick the maps a wall screen should cycle through. The wall view is dark, full screen and refreshes on its own; open the link on the wall PC once and leave it running.</DialogDescription>
        <div className="max-h-64 space-y-0.5 overflow-y-auto rounded-lg border border-border p-1">
          {maps.map((m) => (
            <label key={m.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-surface2">
              <input type="checkbox" checked={picked.includes(m.id)} onChange={() => toggle(m.id)} className="accent-primary" />
              <span className="min-w-0 flex-1 truncate text-text">{m.name}</span>
              {picked.includes(m.id) && <span className="font-mono text-2xs text-muted">#{picked.indexOf(m.id) + 1}</span>}
              <span className="font-mono text-2xs text-muted">{m.total} nodes</span>
            </label>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-2">
            <span className="text-muted">Show each map for</span>
            <select value={sec} onChange={(e) => setSec(Number(e.target.value))} className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-text">
              {INTERVALS.map((s) => <option key={s} value={s}>{s < 60 ? `${s} s` : `${s / 60} min`}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-text2"><input type="checkbox" checked={attention} onChange={(e) => setAttention(e.target.checked)} className="accent-primary" /> Attention list</label>
        </div>
        {url && (
          <div className="flex items-center gap-1 rounded-md border border-border bg-surface2/50 p-1.5">
            <code className="min-w-0 flex-1 truncate font-mono text-2xs text-text2" title={url}>{url}</code>
            <button type="button" className="rounded p-1 text-muted hover:text-text" title="Copy link" onClick={() => { void navigator.clipboard?.writeText(url).then(() => toast.success('Link copied'), () => toast.error('Copy failed')) }}><Copy className="h-3.5 w-3.5" /></button>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
          <Button disabled={!url} onClick={() => { window.open(url, '_blank'); onClose() }}><ExternalLink className="mr-1 h-3.5 w-3.5" /> Open wall</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
