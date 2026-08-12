import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { Route as RouteIcon, Plus, Play, Waypoints, Activity, AlertTriangle, GitBranch, Search } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { useCan } from '@/stores/auth'
import { netpathApi } from './api'
import type { Probe } from './types'
import { StatusBadge, fmtMs, fmtPct, relTime, PROTO_LABEL, SEV_HEX } from './helpers'

function Kpi({ label, value, icon: Icon, tone }: { label: string; value: React.ReactNode; icon: any; tone?: string }) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-surface2" style={{ color: tone }}>
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <div className="text-2xl font-semibold text-text">{value}</div>
          <div className="text-xs text-muted">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

export function ProbesPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const can = useCan()
  const canManage = can('netpath.manage')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)

  const summary = useQuery({ queryKey: ['netpath', 'summary'], queryFn: netpathApi.summary, refetchInterval: 20_000 })
  const probes = useQuery({
    queryKey: ['netpath', 'probes', q],
    queryFn: () => netpathApi.probes(q ? { q } : undefined),
    refetchInterval: 20_000,
    placeholderData: keepPreviousData,
  })

  const runNow = useMutation({
    mutationFn: (id: string) => netpathApi.runNow(id),
    onSuccess: () => toast({ kind: 'success', title: 'Trace queued', description: 'The probe will run on the next scheduler tick.' }),
  })

  const s = summary.data
  const list = probes.data?.data || []

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Waypoints className="h-6 w-6 text-primary" />
            NetPath
          </h1>
          <p className="mt-1 text-sm text-muted">
            Hop-by-hop path monitoring — see exactly where a journey to any service degrades, inside your network or across the internet.
          </p>
        </div>
        {canManage && (
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button><Plus className="mr-1.5 h-4 w-4" /> New probe</Button>
            </DialogTrigger>
            <CreateProbeDialog onClose={() => setOpen(false)} onCreated={(p) => { setOpen(false); navigate(`/netpath/probes/${p.id}`) }} />
          </Dialog>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Kpi label="Probes" value={s?.total_probes ?? '—'} icon={RouteIcon} tone="rgb(var(--primary))" />
        <Kpi label="Healthy" value={s?.ok ?? '—'} icon={Activity} tone="#22c55e" />
        <Kpi label="Degraded" value={s?.degraded ?? '—'} icon={AlertTriangle} tone="#f59e0b" />
        <Kpi label="Unreachable" value={s?.unreachable ?? '—'} icon={AlertTriangle} tone="#ef4444" />
        <Kpi label="Route changes (24h)" value={s?.path_changes_24h ?? '—'} icon={GitBranch} tone="#a855f7" />
      </div>

      <div className="flex items-center gap-2">
        <div className="relative max-w-xs flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search probes…" className="pl-8" />
        </div>
      </div>

      {probes.isLoading ? (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-32 w-full" />)}
        </div>
      ) : list.length === 0 ? (
        <Card><CardContent className="flex flex-col items-center gap-2 py-16 text-center">
          <Waypoints className="h-10 w-10 text-muted" />
          <div className="text-sm font-medium text-text">No probes yet</div>
          <div className="max-w-md text-sm text-muted">Create a probe to a service (a website, DNS resolver, VPN endpoint or internal server) and NetPath will map every hop on the way and watch it over time.</div>
          {canManage && <Button className="mt-2" onClick={() => setOpen(true)}><Plus className="mr-1.5 h-4 w-4" /> New probe</Button>}
        </CardContent></Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {list.map((p) => (
            <Card key={p.id} className="group cursor-pointer transition-colors hover:border-primary/50"
              onClick={() => navigate(`/netpath/probes/${p.id}`)}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate font-semibold text-text">{p.name}</div>
                    <div className="truncate text-xs text-muted">
                      {p.target_host}{p.target_port ? `:${p.target_port}` : ''} · {PROTO_LABEL[p.protocol]}
                    </div>
                  </div>
                  <StatusBadge status={p.last_status} />
                </div>
                <div className="mt-3 grid grid-cols-4 gap-2 text-center">
                  <Metric label="Latency" value={fmtMs(p.last_rtt_ms)} />
                  <Metric label="Loss" value={fmtPct(p.last_loss_pct)} />
                  <Metric label="Hops" value={p.last_hop_count ?? '—'} />
                  <Metric label="Routes" value={p.last_num_paths ?? '—'} />
                </div>
                <div className="mt-3 flex items-center justify-between">
                  <span className="text-[11px] text-muted">{p.last_run_at ? `checked ${relTime(p.last_run_at)}` : 'never run'}</span>
                  {canManage && (
                    <Button size="sm" variant="ghost" className="h-7 px-2 opacity-0 group-hover:opacity-100"
                      onClick={(e) => { e.stopPropagation(); runNow.mutate(p.id) }}>
                      <Play className="mr-1 h-3.5 w-3.5" /> Run
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {s && s.recent_events.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <div className="mb-2 text-sm font-semibold text-text">Recent activity</div>
            <div className="space-y-1.5">
              {s.recent_events.map((e) => (
                <div key={e.id} className="flex items-center gap-2 text-sm">
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: SEV_HEX[e.severity] || '#94a3b8' }} />
                  <button className="truncate text-left text-primary hover:underline"
                    onClick={() => navigate(`/netpath/probes/${e.probe_id}`)}>{e.probe_name}</button>
                  <span className="text-muted">{eventLabel(e.event_type)}</span>
                  <span className="ml-auto shrink-0 text-xs text-muted">{relTime(e.created_at)}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md bg-surface2/50 py-1.5">
      <div className="text-sm font-semibold text-text">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
    </div>
  )
}

export function eventLabel(t: string): string {
  return ({
    path_change: 'route changed', unreachable: 'became unreachable', reachable: 'recovered',
    latency: 'latency breach', loss: 'packet loss',
  } as Record<string, string>)[t] || t
}

function CreateProbeDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Probe) => void }) {
  const [form, setForm] = useState({
    name: '', target_host: '', protocol: 'icmp', target_port: '',
    interval_s: '300', flows: '4', max_hops: '30', probes_per_hop: '3',
    rtt_warn_ms: '150', rtt_crit_ms: '400', loss_warn_pct: '2', loss_crit_pct: '10',
    tags: '',
  })
  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }))
  const qc = useQueryClient()
  const create = useMutation({
    mutationFn: () => netpathApi.createProbe({
      name: form.name.trim(),
      target_host: form.target_host.trim(),
      protocol: form.protocol,
      target_port: form.target_port ? Number(form.target_port) : null,
      interval_s: Number(form.interval_s),
      flows: Number(form.flows),
      max_hops: Number(form.max_hops),
      probes_per_hop: Number(form.probes_per_hop),
      rtt_warn_ms: Number(form.rtt_warn_ms), rtt_crit_ms: Number(form.rtt_crit_ms),
      loss_warn_pct: Number(form.loss_warn_pct), loss_crit_pct: Number(form.loss_crit_pct),
      tags: form.tags ? form.tags.split(',').map((t) => t.trim()).filter(Boolean) : [],
    }),
    onSuccess: (p) => { qc.invalidateQueries({ queryKey: ['netpath'] }); toast({ kind: 'success', title: 'Probe created' }); onCreated(p) },
    onError: (e: any) => toast({ kind: 'error', title: 'Could not create probe', description: e?.response?.data?.detail || String(e) }),
  })
  const needsPort = form.protocol === 'tcp'
  const valid = form.name.trim() && form.target_host.trim() && (!needsPort || form.target_port)

  return (
    <DialogContent className="max-w-lg">
      <DialogHeader><DialogTitle>New NetPath probe</DialogTitle></DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label="Name" required className="sm:col-span-2">
          <Input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="Office → Microsoft 365" />
        </FormField>
        <FormField label="Target host / IP" required>
          <Input value={form.target_host} onChange={(e) => set('target_host', e.target.value)} placeholder="outlook.office365.com" />
        </FormField>
        <FormField label="Protocol">
          <Select value={form.protocol} onValueChange={(v) => set('protocol', v)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="icmp">ICMP echo</SelectItem>
              <SelectItem value="tcp">TCP SYN (through firewalls)</SelectItem>
              <SelectItem value="udp">UDP</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
        {form.protocol !== 'icmp' && (
          <FormField label={`Port${needsPort ? '' : ' (optional)'}`} required={needsPort}>
            <Input type="number" value={form.target_port} onChange={(e) => set('target_port', e.target.value)} placeholder={needsPort ? '443' : '33434'} />
          </FormField>
        )}
        <FormField label="Interval">
          <Select value={form.interval_s} onValueChange={(v) => set('interval_s', v)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="60">Every minute</SelectItem>
              <SelectItem value="120">Every 2 minutes</SelectItem>
              <SelectItem value="300">Every 5 minutes</SelectItem>
              <SelectItem value="600">Every 10 minutes</SelectItem>
              <SelectItem value="900">Every 15 minutes</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="ECMP flows" hint="Higher finds more parallel routes">
          <Input type="number" value={form.flows} onChange={(e) => set('flows', e.target.value)} />
        </FormField>
        <FormField label="Max hops"><Input type="number" value={form.max_hops} onChange={(e) => set('max_hops', e.target.value)} /></FormField>
        <FormField label="Probes / hop" hint="For loss measurement"><Input type="number" value={form.probes_per_hop} onChange={(e) => set('probes_per_hop', e.target.value)} /></FormField>
        <FormField label="Latency warn / crit (ms)">
          <div className="flex gap-2">
            <Input type="number" value={form.rtt_warn_ms} onChange={(e) => set('rtt_warn_ms', e.target.value)} />
            <Input type="number" value={form.rtt_crit_ms} onChange={(e) => set('rtt_crit_ms', e.target.value)} />
          </div>
        </FormField>
        <FormField label="Loss warn / crit (%)">
          <div className="flex gap-2">
            <Input type="number" value={form.loss_warn_pct} onChange={(e) => set('loss_warn_pct', e.target.value)} />
            <Input type="number" value={form.loss_crit_pct} onChange={(e) => set('loss_crit_pct', e.target.value)} />
          </div>
        </FormField>
        <FormField label="Tags (comma separated)" className="sm:col-span-2">
          <Input value={form.tags} onChange={(e) => set('tags', e.target.value)} placeholder="wan, saas" />
        </FormField>
      </div>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button disabled={!valid || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? 'Creating…' : 'Create probe'}
        </Button>
      </DialogFooter>
    </DialogContent>
  )
}
