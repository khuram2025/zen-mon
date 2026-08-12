import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis, Tooltip as RTooltip, CartesianGrid, ReferenceDot,
} from 'recharts'
import {
  ArrowLeft, Play, Trash2, Settings2, ExternalLink, X, GitCompare, Server as ServerIcon,
} from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { useCan } from '@/stores/auth'
import { netpathApi } from './api'
import type { HopNode, Probe, SnapshotSummary } from './types'
import { PathGraph } from './PathGraph'
import { TimelineStrip } from './TimelineStrip'
import { HopHeatmap } from './HopHeatmap'
import {
  StatusBadge, fmtMs, fmtPct, fmtClock, relTime, flag, PROTO_LABEL, INTERNAL_HEX,
} from './helpers'
import { eventLabel } from './ProbesPage'

const WINDOWS = [{ v: 1, l: 'Last hour' }, { v: 6, l: 'Last 6h' }, { v: 24, l: 'Last 24h' }, { v: 168, l: 'Last 7 days' }]

export function PathDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('netpath.manage')

  const [hours, setHours] = useState(24)
  const [selId, setSelId] = useState<number | null>(null)
  const [node, setNode] = useState<HopNode | null>(null)
  const [tab, setTab] = useState('path')
  const [editing, setEditing] = useState(false)
  const [confirmDel, setConfirmDel] = useState(false)

  const probeQ = useQuery({ queryKey: ['netpath', 'probe', id], queryFn: () => netpathApi.probe(id) })
  const snapsQ = useQuery({
    queryKey: ['netpath', 'snaps', id, hours],
    queryFn: () => netpathApi.snapshots(id, hours),
    refetchInterval: 20_000, placeholderData: keepPreviousData,
  })
  const snaps = snapsQ.data?.data || []
  const effectiveSel = selId ?? (snaps.length ? snaps[snaps.length - 1].id : null)

  const pathQ = useQuery({
    queryKey: ['netpath', 'path', id, effectiveSel],
    queryFn: () => netpathApi.path(id, effectiveSel ? { snapshot_id: effectiveSel } : undefined),
    enabled: !!id, placeholderData: keepPreviousData,
  })

  const runNow = useMutation({
    mutationFn: () => netpathApi.runNow(id),
    onSuccess: () => toast({ kind: 'success', title: 'Trace queued' }),
  })
  const del = useMutation({
    mutationFn: () => netpathApi.deleteProbe(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['netpath'] }); navigate('/netpath') },
  })

  const probe = probeQ.data
  const graph = pathQ.data
  const selSnap = graph?.snapshot

  if (probeQ.isLoading) return <div className="space-y-4"><Skeleton className="h-8 w-64" /><Skeleton className="h-96 w-full" /></div>
  if (!probe) return <div className="text-sm text-muted">Probe not found.</div>

  return (
    <div className="space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to="/netpath" className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text">
            <ArrowLeft className="h-3.5 w-3.5" /> All probes
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            {probe.name} <StatusBadge status={probe.last_status} />
          </h1>
          <div className="mt-0.5 text-sm text-muted">
            {probe.target_host}{probe.target_port ? `:${probe.target_port}` : ''}
            {probe.target_ip && probe.target_ip !== probe.target_host ? ` (${probe.target_ip})` : ''}
            {' · '}{PROTO_LABEL[probe.protocol]} · every {probe.interval_s < 60 ? `${probe.interval_s}s` : `${Math.round(probe.interval_s / 60)}m`} · {probe.flows} flows
          </div>
        </div>
        {canManage && (
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={() => runNow.mutate()}><Play className="mr-1.5 h-4 w-4" /> Run now</Button>
            <Button variant="ghost" size="sm" onClick={() => setEditing(true)}><Settings2 className="h-4 w-4" /></Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirmDel(true)}><Trash2 className="h-4 w-4 text-danger" /></Button>
          </div>
        )}
      </div>

      {/* KPIs from the selected snapshot */}
      <div className="grid grid-cols-3 gap-3 md:grid-cols-6">
        <Kpi label="Latency" value={fmtMs(selSnap?.rtt_ms)} />
        <Kpi label="Loss" value={fmtPct(selSnap?.loss_pct)} />
        <Kpi label="Jitter" value={fmtMs(selSnap?.jitter_ms)} />
        <Kpi label="Hops" value={selSnap?.hop_count ?? '—'} />
        <Kpi label="Routes" value={selSnap?.num_paths ?? '—'} />
        <Kpi label="Checked" value={selSnap ? relTime(selSnap.run_at) : '—'} />
      </div>

      {/* time travel */}
      <Card>
        <CardContent className="p-4">
          <div className="mb-1 flex items-center justify-between">
            <div className="text-sm font-medium text-text">
              Path history
              {selSnap && <span className="ml-2 text-xs font-normal text-muted">showing {fmtClock(selSnap.run_at)}{selSnap.path_changed ? ' · route changed here' : ''}</span>}
            </div>
            <Select value={String(hours)} onValueChange={(v) => setHours(Number(v))}>
              <SelectTrigger className="h-8 w-36"><SelectValue /></SelectTrigger>
              <SelectContent>{WINDOWS.map((w) => <SelectItem key={w.v} value={String(w.v)}>{w.l}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          <TimelineStrip snapshots={snaps} selectedId={effectiveSel} onSelect={(s) => { setSelId(s.id); setNode(null) }} />
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="path">Path map</TabsTrigger>
          <TabsTrigger value="latency">Latency</TabsTrigger>
          <TabsTrigger value="hops">Per-hop</TabsTrigger>
          <TabsTrigger value="routes">Routes</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
          <TabsTrigger value="compare">Compare</TabsTrigger>
        </TabsList>

        <TabsContent value="path" className="mt-3">
          {!graph || !graph.snapshot ? (
            <EmptyState msg="No trace has completed for this probe yet. Runs appear here within a minute of the first cycle." />
          ) : (
            <div className="grid gap-3 lg:grid-cols-[1fr_300px]">
              <div className="space-y-2">
                <PathGraph data={graph} selectedIp={node?.ip || null} onSelect={setNode} />
                <Legend />
              </div>
              <NodePanel node={node} onClose={() => setNode(null)} target={graph.target} asGroups={graph.as_groups} snapshot={graph.snapshot} />
            </div>
          )}
        </TabsContent>

        <TabsContent value="latency" className="mt-3">
          <LatencyChart snapshots={snaps} probe={probe} onPick={(sid) => { setSelId(sid); setTab('path') }} />
        </TabsContent>

        <TabsContent value="hops" className="mt-3">
          <HopHeatmapTab id={id} hours={hours} />
        </TabsContent>

        <TabsContent value="routes" className="mt-3">
          <RoutesTab id={id} />
        </TabsContent>

        <TabsContent value="events" className="mt-3">
          <EventsTab id={id} onOpen={(sid) => { if (sid) { setSelId(sid); setTab('path') } }} />
        </TabsContent>

        <TabsContent value="compare" className="mt-3">
          <CompareTab id={id} snapshots={snaps} selected={effectiveSel} />
        </TabsContent>
      </Tabs>

      {editing && <EditDialog probe={probe} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); qc.invalidateQueries({ queryKey: ['netpath', 'probe', id] }) }} />}
      <ConfirmDialog open={confirmDel} onOpenChange={setConfirmDel} title="Delete probe?"
        description={`This removes "${probe.name}" and all its path history.`}
        confirmText="Delete" destructive onConfirm={() => del.mutate()} />
    </div>
  )
}

function Kpi({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Card><CardContent className="p-3">
      <div className="text-lg font-semibold text-text">{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
    </CardContent></Card>
  )
}

function EmptyState({ msg }: { msg: string }) {
  return <Card><CardContent className="py-16 text-center text-sm text-muted">{msg}</CardContent></Card>
}

function Legend() {
  const item = (color: string, label: string, ring?: boolean, dashed?: boolean) => (
    <span className="flex items-center gap-1.5">
      <span className="inline-block h-3 w-3 rounded-full" style={{ background: dashed ? 'transparent' : color, border: ring ? '2px solid #ef4444' : dashed ? '1.5px dashed #94a3b8' : undefined }} />
      {label}
    </span>
  )
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 px-1 text-[11px] text-muted">
      {item('#22c55e', 'healthy')}{item('#f59e0b', 'degraded')}{item('#ef4444', 'high loss')}
      {item('#22c55e', 'packet loss', true)}
      <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-3 rounded-full border-2" style={{ borderColor: INTERNAL_HEX, background: '#22c55e' }} /> your device</span>
      {item('#94a3b8', 'no reply (hidden hop)', false, true)}
      <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-5" style={{ background: '#64748b' }} /> link · thickness = transit</span>
    </div>
  )
}

function NodePanel({ node, onClose, target, asGroups, snapshot }: any) {
  if (!node) {
    return (
      <Card><CardContent className="space-y-3 p-4 text-sm">
        <div className="font-semibold text-text">Path summary</div>
        <div className="text-muted">Click any hop to inspect it — reverse DNS, ASN/owner, geolocation, and a link to the device if it's one you monitor.</div>
        <div className="space-y-1.5 border-t border-border pt-3">
          <Row k="Destination" v={`${target.host}${target.reached ? '' : ' (unreachable)'}`} />
          <Row k="Route hash" v={snapshot?.path_hash != null ? String(snapshot.path_hash) : '—'} />
          <Row k="Networks crossed" v={String(asGroups?.length ?? 0)} />
        </div>
        {asGroups?.length > 0 && (
          <div className="space-y-1 border-t border-border pt-3">
            <div className="text-xs font-medium text-muted">Autonomous systems</div>
            {asGroups.map((g: any) => (
              <div key={g.asn} className="flex items-center justify-between text-xs">
                <span className="truncate text-text">{g.as_name || `AS${g.asn}`}</span>
                <span className="text-muted">{g.count} hop{g.count > 1 ? 's' : ''}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent></Card>
    )
  }
  return (
    <Card><CardContent className="space-y-2 p-4 text-sm">
      <div className="flex items-start justify-between">
        <div className="font-semibold text-text">{node.is_dest ? '🎯 Destination' : `Hop`}</div>
        <button onClick={onClose}><X className="h-4 w-4 text-muted hover:text-text" /></button>
      </div>
      <div className="break-all font-mono text-xs text-text">{node.ip}</div>
      {node.hostname && <div className="break-all text-xs text-muted">{node.hostname}</div>}
      <div className="space-y-1.5 border-t border-border pt-2">
        <Row k="RTT (avg)" v={fmtMs(node.rtt_avg)} />
        <Row k="RTT range" v={`${fmtMs(node.rtt_min)} – ${fmtMs(node.rtt_max)}`} />
        <Row k="Loss" v={`${fmtPct(node.loss_pct)} (${node.recv}/${node.sent})`} />
        <Row k="Scope" v={node.is_internal ? 'Internal' : 'External'} />
        {node.asn && <Row k="AS" v={`${node.as_name || ''} (AS${node.asn})`} />}
        {node.country && <Row k="Country" v={`${flag(node.country)} ${node.country}`} />}
        <Row k="Flows via" v={String(node.flow_count)} />
      </div>
      {node.device_id && (
        <Link to={`/devices/${node.device_id}`}
          className="mt-1 flex items-center gap-1.5 rounded-md bg-primary/10 px-2 py-1.5 text-xs font-medium text-primary hover:bg-primary/20">
          <ServerIcon className="h-3.5 w-3.5" /> {node.device_name || 'Monitored device'} <ExternalLink className="ml-auto h-3 w-3" />
        </Link>
      )}
    </CardContent></Card>
  )
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex items-center justify-between gap-2"><span className="text-muted">{k}</span><span className="text-right text-text">{v}</span></div>
}

function LatencyChart({ snapshots, probe, onPick }: { snapshots: SnapshotSummary[]; probe: Probe; onPick: (id: number) => void }) {
  const data = useMemo(() => snapshots.map((s) => ({
    id: s.id, t: new Date(s.run_at).getTime(),
    rtt: s.reached ? s.rtt_ms : null, loss: s.loss_pct ?? 0, changed: s.path_changed, reached: s.reached,
  })), [snapshots])
  if (!snapshots.length) return <EmptyState msg="No latency history in this window." />
  const changes = data.filter((d) => d.changed)
  return (
    <Card><CardContent className="p-4">
      <div className="h-72 w-full">
        <ResponsiveContainer>
          <ComposedChart data={data} onClick={(e: any) => { const p = e?.activePayload?.[0]?.payload; if (p) onPick(p.id) }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" />
            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time"
              tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              stroke="rgb(var(--muted))" fontSize={11} />
            <YAxis yAxisId="l" stroke="rgb(var(--muted))" fontSize={11} unit="ms" />
            <YAxis yAxisId="r" orientation="right" stroke="rgb(var(--muted))" fontSize={11} unit="%" domain={[0, 100]} />
            <RTooltip contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
              labelFormatter={(t) => fmtClock(new Date(t as number).toISOString())}
              formatter={(v: any, n: string) => [n === 'loss' ? `${v}%` : `${v} ms`, n === 'loss' ? 'Loss' : 'Latency']} />
            <Area yAxisId="r" dataKey="loss" fill="#ef4444" stroke="#ef4444" fillOpacity={0.12} />
            <Line yAxisId="l" dataKey="rtt" stroke="rgb(var(--primary))" dot={false} strokeWidth={2} connectNulls={false} />
            {changes.map((c) => <ReferenceDot key={c.id} yAxisId="l" x={c.t} y={c.rtt ?? 0} r={4} fill="#a855f7" stroke="none" />)}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-1 text-center text-[11px] text-muted">Click a point to open that moment on the path map · <span style={{ color: '#a855f7' }}>◆</span> route change</div>
    </CardContent></Card>
  )
}

function HopHeatmapTab({ id, hours }: { id: string; hours: number }) {
  const q = useQuery({ queryKey: ['netpath', 'hops', id, hours], queryFn: () => netpathApi.hops(id, hours) })
  if (q.isLoading) return <Skeleton className="h-64 w-full" />
  return <Card><CardContent className="p-4">{q.data && <HopHeatmap times={q.data.times} ladder={q.data.ladder} />}</CardContent></Card>
}

function RoutesTab({ id }: { id: string }) {
  const q = useQuery({ queryKey: ['netpath', 'routes', id], queryFn: () => netpathApi.paths(id) })
  if (q.isLoading) return <Skeleton className="h-40 w-full" />
  const rows = q.data?.data || []
  if (!rows.length) return <EmptyState msg="No distinct routes recorded yet." />
  return (
    <div className="space-y-2">
      {rows.map((p) => (
        <Card key={p.id}><CardContent className="p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              {p.as_path.length ? p.as_path.map((a, i) => (
                <span key={i} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-muted">→</span>}
                  <Badge variant="outline">{a.as_name || `AS${a.asn}`}</Badge>
                </span>
              )) : <span className="text-xs text-muted">{p.hop_ips.slice(0, 4).join(' → ')}…</span>}
            </div>
            <div className="flex items-center gap-3 text-xs text-muted">
              <span>{p.hop_count} hops</span>
              <span>seen {p.seen_count}×</span>
              <span>last {relTime(p.last_seen)}</span>
            </div>
          </div>
        </CardContent></Card>
      ))}
    </div>
  )
}

function EventsTab({ id, onOpen }: { id: string; onOpen: (sid: number | null) => void }) {
  const q = useQuery({ queryKey: ['netpath', 'events', id], queryFn: () => netpathApi.events(id) })
  if (q.isLoading) return <Skeleton className="h-40 w-full" />
  const rows = q.data?.data || []
  if (!rows.length) return <EmptyState msg="No events recorded yet." />
  const sev: Record<string, string> = { info: '#3b82f6', warning: '#f59e0b', critical: '#ef4444' }
  return (
    <Card><CardContent className="p-0">
      {rows.map((e) => (
        <button key={e.id} onClick={() => onOpen(e.snapshot_id)}
          className="flex w-full items-center gap-3 border-b border-border/50 px-4 py-2.5 text-left text-sm last:border-0 hover:bg-surface2/40">
          <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: sev[e.severity] || '#94a3b8' }} />
          <span className="font-medium text-text">{eventLabel(e.event_type)}</span>
          <span className="truncate text-muted">
            {e.event_type === 'path_change' && e.details?.hop_count ? `${e.details.hop_count} hops, ${e.details.num_paths} route(s)` : ''}
          </span>
          <span className="ml-auto shrink-0 text-xs text-muted">{fmtClock(e.created_at)}</span>
        </button>
      ))}
    </CardContent></Card>
  )
}

function CompareTab({ id, snapshots, selected }: { id: string; snapshots: SnapshotSummary[]; selected: number | null }) {
  // default: compare the selected snapshot against the most recent one before a route change
  const defaults = useMemo(() => {
    if (snapshots.length < 2) return { a: null, b: null }
    const b = selected ?? snapshots[snapshots.length - 1].id
    const bi = snapshots.findIndex((s) => s.id === b)
    // find a prior snapshot with a different route (before the nearest change)
    let a = snapshots[0].id
    for (let i = bi - 1; i >= 0; i--) { if (snapshots[i + 1]?.path_changed) { a = snapshots[i].id; break } }
    return { a, b }
  }, [snapshots, selected])
  const [a, setA] = useState<number | null>(null)
  const [b, setB] = useState<number | null>(null)
  const av = a ?? defaults.a, bv = b ?? defaults.b
  const q = useQuery({
    queryKey: ['netpath', 'compare', id, av, bv],
    queryFn: () => netpathApi.compare(id, av!, bv!),
    enabled: !!av && !!bv && av !== bv,
  })
  if (snapshots.length < 2) return <EmptyState msg="Need at least two snapshots to compare." />
  const opts = snapshots
  const cmp = q.data
  return (
    <div className="space-y-3">
      <Card><CardContent className="flex flex-wrap items-center gap-3 p-3 text-sm">
        <GitCompare className="h-4 w-4 text-muted" />
        <PickSnap label="A" value={av} onChange={setA} opts={opts} />
        <span className="text-muted">vs</span>
        <PickSnap label="B" value={bv} onChange={setB} opts={opts} />
        {cmp && (
          <span className="ml-auto flex items-center gap-3 text-xs">
            <Badge variant={cmp.summary.identical ? 'success' : 'warning'}>{cmp.summary.identical ? 'Identical route' : 'Route differs'}</Badge>
            <span className="text-muted">Δ latency {cmp.summary.rtt_delta > 0 ? '+' : ''}{cmp.summary.rtt_delta} ms</span>
          </span>
        )}
      </CardContent></Card>
      {q.isLoading ? <Skeleton className="h-64 w-full" /> : cmp && (
        <Card><CardContent className="p-4">
          {(cmp.summary.hops_added.length > 0 || cmp.summary.hops_removed.length > 0) && (
            <div className="mb-3 flex flex-wrap gap-4 text-xs">
              {cmp.summary.hops_removed.length > 0 && <div><span className="font-medium text-danger">Removed:</span> {cmp.summary.hops_removed.map((h) => h.hostname || h.ip).join(', ')}</div>}
              {cmp.summary.hops_added.length > 0 && <div><span className="font-medium text-success">Added:</span> {cmp.summary.hops_added.map((h) => h.hostname || h.ip).join(', ')}</div>}
            </div>
          )}
          <div className="space-y-1">
            {cmp.rows.map((r) => (
              <div key={r.ttl} className="flex items-start gap-3 border-b border-border/40 py-1.5 text-sm last:border-0">
                <span className="mt-0.5 inline-flex h-5 w-7 shrink-0 items-center justify-center rounded bg-surface2 text-[11px] font-semibold text-muted">{r.ttl}</span>
                <div className="flex flex-1 flex-wrap gap-1.5">
                  {r.same.map((h) => <Chip key={'s' + h.ip} tone="same" label={h.hostname || h.ip} sub={h.as_name} />)}
                  {r.removed.map((h) => <Chip key={'r' + h.ip} tone="removed" label={h.hostname || h.ip} sub={h.as_name} />)}
                  {r.added.map((h) => <Chip key={'a' + h.ip} tone="added" label={h.hostname || h.ip} sub={h.as_name} />)}
                </div>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-4 text-[11px] text-muted">
            <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-success" />added in B</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-danger" />only in A</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-muted" />unchanged</span>
          </div>
        </CardContent></Card>
      )}
    </div>
  )
}

function PickSnap({ label, value, onChange, opts }: { label: string; value: number | null; onChange: (v: number) => void; opts: SnapshotSummary[] }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs font-medium text-muted">{label}</span>
      <Select value={value ? String(value) : ''} onValueChange={(v) => onChange(Number(v))}>
        <SelectTrigger className="h-8 w-48"><SelectValue placeholder="pick…" /></SelectTrigger>
        <SelectContent>
          {opts.map((s) => <SelectItem key={s.id} value={String(s.id)}>{fmtClock(s.run_at)}{s.path_changed ? ' ⟳' : ''}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  )
}

function Chip({ tone, label, sub }: { tone: 'same' | 'added' | 'removed'; label: string; sub?: string | null }) {
  const cls = tone === 'added' ? 'border-success/40 bg-success/10 text-success'
    : tone === 'removed' ? 'border-danger/40 bg-danger/10 text-danger line-through'
    : 'border-border bg-surface2/50 text-text'
  return <span className={`rounded border px-1.5 py-0.5 text-xs ${cls}`} title={sub || ''}>{label}</span>
}

function EditDialog({ probe, onClose, onSaved }: { probe: Probe; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    name: probe.name, target_host: probe.target_host,
    target_port: probe.target_port ? String(probe.target_port) : '',
    interval_s: String(probe.interval_s), flows: String(probe.flows),
    max_hops: String(probe.max_hops), probes_per_hop: String(probe.probes_per_hop),
    rtt_warn_ms: String(probe.rtt_warn_ms), rtt_crit_ms: String(probe.rtt_crit_ms),
    loss_warn_pct: String(probe.loss_warn_pct), loss_crit_pct: String(probe.loss_crit_pct),
    enabled: probe.enabled,
  })
  const set = (k: string, v: any) => setF((s) => ({ ...s, [k]: v }))
  const save = useMutation({
    mutationFn: () => netpathApi.updateProbe(probe.id, {
      name: f.name.trim(), target_host: f.target_host.trim(),
      target_port: f.target_port ? Number(f.target_port) : null,
      interval_s: Number(f.interval_s), flows: Number(f.flows),
      max_hops: Number(f.max_hops), probes_per_hop: Number(f.probes_per_hop),
      rtt_warn_ms: Number(f.rtt_warn_ms), rtt_crit_ms: Number(f.rtt_crit_ms),
      loss_warn_pct: Number(f.loss_warn_pct), loss_crit_pct: Number(f.loss_crit_pct),
      enabled: f.enabled,
    }),
    onSuccess: () => { toast({ kind: 'success', title: 'Saved' }); onSaved() },
    onError: (e: any) => toast({ kind: 'error', title: 'Save failed', description: e?.response?.data?.detail || String(e) }),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Probe settings</DialogTitle></DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Name" className="sm:col-span-2"><Input value={f.name} onChange={(e) => set('name', e.target.value)} /></FormField>
          <FormField label="Target host / IP"><Input value={f.target_host} onChange={(e) => set('target_host', e.target.value)} /></FormField>
          <FormField label="Port"><Input type="number" value={f.target_port} onChange={(e) => set('target_port', e.target.value)} /></FormField>
          <FormField label="Interval (s)"><Input type="number" value={f.interval_s} onChange={(e) => set('interval_s', e.target.value)} /></FormField>
          <FormField label="ECMP flows"><Input type="number" value={f.flows} onChange={(e) => set('flows', e.target.value)} /></FormField>
          <FormField label="Max hops"><Input type="number" value={f.max_hops} onChange={(e) => set('max_hops', e.target.value)} /></FormField>
          <FormField label="Probes / hop"><Input type="number" value={f.probes_per_hop} onChange={(e) => set('probes_per_hop', e.target.value)} /></FormField>
          <FormField label="Latency warn / crit (ms)">
            <div className="flex gap-2"><Input type="number" value={f.rtt_warn_ms} onChange={(e) => set('rtt_warn_ms', e.target.value)} /><Input type="number" value={f.rtt_crit_ms} onChange={(e) => set('rtt_crit_ms', e.target.value)} /></div>
          </FormField>
          <FormField label="Loss warn / crit (%)">
            <div className="flex gap-2"><Input type="number" value={f.loss_warn_pct} onChange={(e) => set('loss_warn_pct', e.target.value)} /><Input type="number" value={f.loss_crit_pct} onChange={(e) => set('loss_crit_pct', e.target.value)} /></div>
          </FormField>
          <div className="flex items-center gap-2 sm:col-span-2">
            <Switch checked={f.enabled} onCheckedChange={(v) => set('enabled', v)} />
            <span className="text-sm text-text">Enabled</span>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
