/** On-demand network flow capture.
 *
 *  Connection-level flow accounting, not packet capture — no driver, no
 *  payloads. The operator picks a window (5 minutes by default), the agent
 *  samples the host's connection table for that long and streams what it
 *  sees, and this view follows the run live and then lets you search it. */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, ArrowDown, ArrowUp, CircleStop, Info,
  Play, Radio, Search,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBytes, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import {
  EmptyState, ExportCsvButton, QueryError, TablePager, TableStateRow, usePagedRows,
} from '@/components/servers/tables'

interface Capture {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'expired'
  interface: string | null
  duration_s: number
  requested_at: string
  started_at: string | null
  ends_at: string | null
  completed_at: string | null
  samples: number
  flow_count: number
  bytes_sent: number
  bytes_received: number
  bytes_available: boolean
  truncated: boolean
  note: string | null
  error_message: string | null
  requested_by_name: string | null
}

interface Flow {
  protocol: string
  local_ip: string
  local_port: number
  remote_ip: string
  remote_port: number
  pid: number
  process_name: string
  service_name: string
  state: string
  bytes_sent: number
  bytes_received: number
  bytes_known: boolean
  first_seen: string
  last_seen: string
  samples: number
}

const DURATIONS = [
  { value: '60', label: '1 minute' },
  { value: '300', label: '5 minutes' },
  { value: '900', label: '15 minutes' },
  { value: '1800', label: '30 minutes' },
  { value: '3600', label: '1 hour' },
]

const FLOW_CSV = [
  { header: 'Protocol', value: (f: Flow) => f.protocol },
  { header: 'Process', value: (f: Flow) => f.process_name },
  { header: 'Service', value: (f: Flow) => f.service_name },
  { header: 'PID', value: (f: Flow) => f.pid },
  { header: 'Source IP', value: (f: Flow) => f.local_ip },
  { header: 'Source port', value: (f: Flow) => f.local_port },
  { header: 'Destination IP', value: (f: Flow) => f.remote_ip },
  { header: 'Destination port', value: (f: Flow) => f.remote_port },
  { header: 'Sent bytes', value: (f: Flow) => (f.bytes_known ? f.bytes_sent : '') },
  { header: 'Received bytes', value: (f: Flow) => (f.bytes_known ? f.bytes_received : '') },
  { header: 'State', value: (f: Flow) => f.state },
  { header: 'First seen', value: (f: Flow) => f.first_seen },
  { header: 'Last seen', value: (f: Flow) => f.last_seen },
]

function statusBadge(s: Capture['status']) {
  if (s === 'running') return <Badge variant="info">Capturing</Badge>
  if (s === 'queued') return <Badge variant="warning">Starting…</Badge>
  if (s === 'completed') return <Badge variant="success">Complete</Badge>
  if (s === 'failed') return <Badge variant="danger">Failed</Badge>
  return <Badge variant="outline">Stopped</Badge>
}

export function NetworkCapturePanel({
  serverId, serverName, interfaces,
}: {
  serverId: string
  serverName: string
  interfaces: string[]
}) {
  const qc = useQueryClient()
  const [duration, setDuration] = useState('300')
  const [iface, setIface] = useState('__all')
  const [selected, setSelected] = useState<string | null>(null)

  const capturesQ = useQuery<{ items: Capture[] }>({
    queryKey: ['servers', serverId, 'captures'],
    queryFn: async () => (await api.get(`/servers/${serverId}/network-captures`)).data,
    // Poll fast while something is in flight so the run appears to stream.
    refetchInterval: (q) => {
      const items = (q.state.data as { items: Capture[] } | undefined)?.items || []
      return items.some((c) => c.status === 'queued' || c.status === 'running') ? 5_000 : 30_000
    },
  })
  const captures = capturesQ.data?.items || []
  const active = captures.find((c) => c.status === 'queued' || c.status === 'running')
  const current = captures.find((c) => c.id === selected) || active || captures[0]

  const start = useMutation({
    mutationFn: async () => (await api.post(`/servers/${serverId}/network-capture`, {
      duration_s: Number(duration),
      interface: iface === '__all' ? null : iface,
    })).data,
    onSuccess: (d) => {
      toast.success('Capture queued', d.detail)
      setSelected(d.id)
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (e) => toast.error('Could not start capture', apiErrorMessage(e)),
  })

  const stop = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/servers/network-captures/${id}`)).data,
    onSuccess: () => {
      toast.success('Capture stopped')
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (e) => toast.error('Could not stop capture', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            <Radio className={cn('h-3.5 w-3.5', active ? 'animate-pulse text-danger' : 'text-muted')} />
            <h3 className="text-sm font-semibold tracking-tight">Traffic capture</h3>
            {active && statusBadge(active.status)}
          </div>
        </div>
        <CardContent className="space-y-3 px-4 pb-4 pt-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="cap-dur">Capture for</Label>
              <Select value={duration} onValueChange={setDuration}>
                <SelectTrigger id="cap-dur" className="h-9 w-[150px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {DURATIONS.map((d) => <SelectItem key={d.value} value={d.value}>{d.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap-if">Interface</Label>
              <Select value={iface} onValueChange={setIface}>
                <SelectTrigger id="cap-if" className="h-9 w-[200px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all">All interfaces</SelectItem>
                  {interfaces.map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            {active ? (
              <Button variant="destructive" onClick={() => stop.mutate(active.id)} disabled={stop.isPending}>
                <CircleStop className="h-4 w-4" /> Stop capture
              </Button>
            ) : (
              <Button onClick={() => start.mutate()} disabled={start.isPending}>
                <Play className="h-4 w-4" />
                {start.isPending ? 'Starting…' : 'Start capture'}
              </Button>
            )}
            {captures.length > 1 && (
              <div className="ml-auto space-y-1.5">
                <Label htmlFor="cap-hist">Viewing</Label>
                <Select value={current?.id || ''} onValueChange={setSelected}>
                  <SelectTrigger id="cap-hist" className="h-9 w-[230px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {captures.map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {new Date(c.requested_at).toLocaleString()} · {c.status}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>

          <p className="flex items-start gap-1.5 text-[11px] text-muted">
            <Info className="mt-0.5 h-3 w-3 shrink-0" />
            Records which processes talked to which addresses, and how much they moved.
            Connection metadata only — no packet payloads are captured or stored.
          </p>
        </CardContent>
      </Card>

      {current && <CaptureDetail capture={current} serverName={serverName} />}
      {!current && !capturesQ.isLoading && (
        <Card>
          <CardContent className="pt-4">
            <EmptyState
              icon={<Activity className="h-7 w-7" />}
              title="No captures yet"
              hint="Start one above to see which processes are using the network and where their traffic is going."
            />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function CaptureDetail({ capture, serverName }: { capture: Capture; serverName: string }) {
  const [q, setQ] = useState('')
  const [protocol, setProtocol] = useState('__all')
  const live = capture.status === 'running' || capture.status === 'queued'

  const flowsQ = useQuery<{ items: Flow[]; total: number; bytes_available: boolean }>({
    queryKey: ['captures', capture.id, 'flows', q, protocol],
    queryFn: async () => (await api.get(`/servers/network-captures/${capture.id}/flows`, {
      params: {
        page_size: 500,
        ...(q.trim() ? { q: q.trim() } : {}),
        ...(protocol !== '__all' ? { protocol } : {}),
      },
    })).data,
    refetchInterval: live ? 5_000 : false,
  })

  const summaryQ = useQuery<{
    top_processes: { process_name: string; service_name: string; bytes_sent: number; bytes_received: number; flows: number }[]
    top_peers: { remote_ip: string; bytes_sent: number; bytes_received: number; flows: number }[]
  }>({
    queryKey: ['captures', capture.id, 'summary'],
    queryFn: async () => (await api.get(`/servers/network-captures/${capture.id}/summary`)).data,
    refetchInterval: live ? 10_000 : false,
  })

  const flows = flowsQ.data?.items || []
  const pager = usePagedRows(flows, 50)

  const elapsed = useMemo(() => {
    if (!capture.started_at) return null
    const end = capture.completed_at ? Date.parse(capture.completed_at) : Date.now()
    return Math.max(0, Math.round((end - Date.parse(capture.started_at)) / 1000))
  }, [capture.started_at, capture.completed_at])

  const progress = capture.duration_s > 0 && elapsed != null
    ? Math.min(100, (elapsed / capture.duration_s) * 100)
    : 0

  return (
    <div className="space-y-4">
      {live && (
        <Card className="overflow-hidden border-info/30 bg-info/5">
          <CardContent className="space-y-2 pt-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Radio className="h-4 w-4 animate-pulse text-info" />
              <span className="font-medium">
                {capture.status === 'queued' ? 'Waiting for the agent to pick this up…' : 'Capture running'}
              </span>
              <span className="text-xs text-muted">
                {elapsed != null ? `${elapsed}s of ${capture.duration_s}s` : `${capture.duration_s}s window`}
                {' · '}{capture.samples} samples{' · '}{flowsQ.data?.total ?? 0} flows so far
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full bg-info transition-all" style={{ width: `${progress}%` }} />
            </div>
          </CardContent>
        </Card>
      )}

      {capture.error_message && (
        <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{capture.error_message}</span>
        </div>
      )}

      {/* Byte attribution needs TCP ESTATS, which needs an elevated agent.
          Without it, zero is "unknown", not "no traffic" — say which. */}
      {!capture.bytes_available && capture.status !== 'queued' && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div>
            <div className="font-medium">Traffic volume unavailable on this host</div>
            <div className="mt-0.5 text-muted">
              {capture.note || 'Per-connection byte counters could not be read.'} Conversations,
              processes and ports below are still accurate — only the byte totals are missing.
              Running the agent as a service (LocalSystem) enables them.
            </div>
          </div>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden">
          <div className="border-b border-border px-4 py-2.5 text-sm font-semibold">Top processes</div>
          <CardContent className="px-0 pb-2 pt-1">
            <TopList
              rows={(summaryQ.data?.top_processes || []).map((p) => ({
                label: p.process_name || `pid ${0}`,
                sub: p.service_name,
                sent: p.bytes_sent, recv: p.bytes_received, flows: p.flows,
              }))}
              bytesKnown={capture.bytes_available}
            />
          </CardContent>
        </Card>
        <Card className="overflow-hidden">
          <div className="border-b border-border px-4 py-2.5 text-sm font-semibold">Top destinations</div>
          <CardContent className="px-0 pb-2 pt-1">
            <TopList
              rows={(summaryQ.data?.top_peers || []).map((p) => ({
                label: p.remote_ip, sub: '',
                sent: p.bytes_sent, recv: p.bytes_received, flows: p.flows,
              }))}
              bytesKnown={capture.bytes_available}
            />
          </CardContent>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <h3 className="text-sm font-semibold tracking-tight">
            Conversations
            <span className="ml-2 text-[10px] font-normal uppercase tracking-wider text-muted">
              {flowsQ.data?.total ?? 0} flows
            </span>
          </h3>
          {statusBadge(capture.status)}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-b border-border/50 px-4 py-3">
          <div className="relative min-w-[240px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-8 pl-8"
              placeholder="Search IP, port, process or service…"
              value={q}
              onChange={(e) => { setQ(e.target.value); pager.reset() }}
            />
          </div>
          <Select value={protocol} onValueChange={(v) => { setProtocol(v); pager.reset() }}>
            <SelectTrigger className="h-8 w-[130px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All protocols</SelectItem>
              <SelectItem value="tcp">TCP</SelectItem>
              <SelectItem value="udp">UDP</SelectItem>
            </SelectContent>
          </Select>
          <ExportCsvButton rows={flows} columns={FLOW_CSV} filename={`${serverName}-capture.csv`} />
        </div>
        <CardContent className="px-0 pb-0 pt-0">
          <div className="overflow-x-auto">
            <Table>
              <THead className="bg-surface2/40">
                <Tr>
                  <Th className="pl-4">Process / service</Th>
                  <Th>Source</Th>
                  <Th>Destination</Th>
                  <Th>Proto</Th>
                  <Th className="text-right">Sent</Th>
                  <Th className="text-right">Received</Th>
                  <Th className="pr-4">Last seen</Th>
                </Tr>
              </THead>
              <TBody>
                {flowsQ.isError ? (
                  <TableStateRow colSpan={7}>
                    <QueryError error={flowsQ.error} onRetry={() => flowsQ.refetch()} />
                  </TableStateRow>
                ) : flowsQ.isLoading ? (
                  <TableStateRow colSpan={7}><div className="py-10"><Skeleton className="h-24 w-full" /></div></TableStateRow>
                ) : flows.length === 0 ? (
                  <TableStateRow colSpan={7}>
                    <EmptyState
                      icon={<Activity className="h-7 w-7" />}
                      title={q || protocol !== '__all' ? 'No flows match this search' : live ? 'Waiting for the first flows…' : 'No conversations recorded'}
                      hint={q || protocol !== '__all'
                        ? 'Clear the filters to see every conversation in this capture.'
                        : live ? 'Flows appear within a few seconds of the capture starting.' : undefined}
                    />
                  </TableStateRow>
                ) : pager.pageRows.map((f, i) => (
                  <Tr key={`${f.protocol}-${f.local_ip}-${f.local_port}-${f.remote_ip}-${f.remote_port}-${f.pid}`}
                      className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                    <Td className="py-2 pl-4">
                      <div className="text-sm font-medium">{f.process_name || '—'}</div>
                      <div className="text-[11px] text-muted">
                        {f.service_name ? `${f.service_name} · ` : ''}pid {f.pid}
                      </div>
                    </Td>
                    <Td className="font-mono text-xs">
                      <span className="text-muted">{f.local_ip}</span>:{f.local_port}
                    </Td>
                    <Td className="font-mono text-xs">
                      <span className="text-text">{f.remote_ip}</span>:<span className="font-semibold">{f.remote_port}</span>
                    </Td>
                    <Td><Badge variant="outline" className="text-[10px] uppercase">{f.protocol}</Badge></Td>
                    <Td className="text-right text-xs tabular-nums">
                      {f.bytes_known
                        ? <span className="inline-flex items-center gap-1"><ArrowUp className="h-3 w-3 text-info" />{formatBytes(f.bytes_sent)}</span>
                        : <span className="text-muted">—</span>}
                    </Td>
                    <Td className="text-right text-xs tabular-nums">
                      {f.bytes_known
                        ? <span className="inline-flex items-center gap-1"><ArrowDown className="h-3 w-3 text-success" />{formatBytes(f.bytes_received)}</span>
                        : <span className="text-muted">—</span>}
                    </Td>
                    <Td className="pr-4 text-xs text-muted">{relativeTime(f.last_seen)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
          <TablePager {...pager} noun="flows" />
        </CardContent>
      </Card>

      {capture.truncated && (
        <p className="text-[11px] text-warning">
          This capture hit its flow limit; the busiest conversations were kept.
          Use a shorter window or a specific interface to narrow it.
        </p>
      )}
    </div>
  )
}

function TopList({
  rows, bytesKnown,
}: {
  rows: { label: string; sub: string; sent: number; recv: number; flows: number }[]
  bytesKnown: boolean
}) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-center text-xs text-muted">Nothing recorded yet</div>
  }
  const max = Math.max(1, ...rows.map((r) => r.sent + r.recv))
  return (
    <div className="space-y-1.5 px-4 py-2">
      {rows.map((r) => {
        const total = r.sent + r.recv
        return (
          <div key={r.label + r.sub}>
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate font-medium">
                {r.label}
                {r.sub && <span className="ml-1 text-[10px] text-muted">{r.sub}</span>}
              </span>
              <span className="shrink-0 tabular-nums text-muted">
                {bytesKnown ? formatBytes(total) : `${r.flows} flow${r.flows === 1 ? '' : 's'}`}
              </span>
            </div>
            {bytesKnown && (
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface2">
                <div className="h-full rounded-full bg-gradient-to-r from-primary to-info"
                     style={{ width: `${(total / max) * 100}%` }} />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
