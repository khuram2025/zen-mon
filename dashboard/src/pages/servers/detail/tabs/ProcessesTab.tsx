/** Processes tab — the agent's top-N process sample, the watchlist, and
 *  per-process CPU / memory history. */

import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, ChevronRight, Cpu, Eye, EyeOff, History, Layers, ListTree, MemoryStick, Search, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatBytes, relativeTime, timeAxisTickFormatter } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { ExportCsvButton, QueryError, sortIndicator, sortableTh } from '@/components/servers/tables'
import type { ServerItem, ServerMetricsResponse, ServerProcess } from '@/types/servers'
import { useProcesses } from '../data'
import {
  C, ChartStats, EmptyState, KeyValues, Meter, Panel, Pill, Segmented, StatTile, TimeSeries,
  fmtPct, pctTone, stats, type Tone,
} from '../ui'

/* ── Helpers ─────────────────────────────────────────────────────── */

/** The API returns at most this many processes, selected by CPU. */
const SAMPLE_LIMIT = 200

/** Row thresholds: a single process at 50% is worth a second look. */
const PROC_WARN = 50
const PROC_CRIT = 80

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-text',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  info: 'text-info',
  muted: 'text-muted',
}

function telemetryEmptyMessage(server: ServerItem): string {
  if (server.agent_status === 'online') {
    return 'The agent is online but is not reporting this data. Check that server monitoring is enabled in its monitoring profile.'
  }
  if (server.agent_id) return 'No recent telemetry from the agent.'
  return 'Install and authorize an agent to collect this data.'
}

type ProcRow = {
  key: string
  name: string
  pid: number | null
  count: number
  cpu_pct: number
  memory_bytes: number
  user_name: string | null
  cmdline: string | null
  started_at: string | null
  state: string
  running: boolean
  watchlisted: boolean
  updated_at: string | null
}

type SortKey = 'name' | 'pid' | 'user' | 'cpu' | 'mem' | 'updated'
type StatusFilter = 'all' | 'watched' | 'missing'

function toRow(p: ServerProcess): ProcRow {
  return {
    key: `${p.pid}-${p.name}`,
    name: p.name,
    pid: p.pid,
    count: 1,
    cpu_pct: p.cpu_pct || 0,
    memory_bytes: p.memory_bytes || 0,
    user_name: p.user_name,
    cmdline: p.cmdline,
    started_at: p.started_at,
    state: p.state || (p.running === false ? 'not_running' : 'running'),
    running: p.running !== false,
    watchlisted: Boolean(p.watchlisted),
    updated_at: p.updated_at,
  }
}

function groupByName(items: ServerProcess[]): ProcRow[] {
  const m = new Map<string, ProcRow>()
  for (const p of items) {
    const g = m.get(p.name)
    if (g) {
      g.count += 1
      g.cpu_pct += p.cpu_pct || 0
      g.memory_bytes += p.memory_bytes || 0
      g.running = g.running || p.running !== false
      g.watchlisted = g.watchlisted || Boolean(p.watchlisted)
      if (p.running !== false) g.state = p.state || 'running'
      if (p.updated_at && (!g.updated_at || p.updated_at > g.updated_at)) g.updated_at = p.updated_at
    } else {
      m.set(p.name, { ...toRow(p), key: p.name, pid: null })
    }
  }
  return [...m.values()]
}

function stateLabel(state: string): string {
  if (state === 'not_running') return 'Not running'
  return state ? state.charAt(0).toUpperCase() + state.slice(1).replace(/_/g, ' ') : 'Unknown'
}

/** series[] → chart rows keyed by epoch ms, one column per metric. */
function seriesRows(resp: ServerMetricsResponse | undefined, keys: Record<string, string>) {
  if (!resp) return []
  const rows = new Map<number, Record<string, number | null>>()
  for (const s of resp.series) {
    const col = keys[s.metric]
    if (!col) continue
    for (const p of s.points) {
      const ts = Date.parse(p.timestamp)
      if (!rows.has(ts)) rows.set(ts, { ts })
      rows.get(ts)![col] = p.value
    }
  }
  return [...rows.values()].sort((a, b) => (a.ts as number) - (b.ts as number))
}

const PROCESSES_CSV = [
  { header: 'Process', value: (p: ProcRow) => p.name },
  { header: 'PID', value: (p: ProcRow) => p.pid },
  { header: 'Instances', value: (p: ProcRow) => p.count },
  { header: 'State', value: (p: ProcRow) => p.state },
  { header: 'Watchlisted', value: (p: ProcRow) => (p.watchlisted ? 'yes' : 'no') },
  { header: 'CPU %', value: (p: ProcRow) => p.cpu_pct.toFixed(1) },
  { header: 'Memory bytes', value: (p: ProcRow) => p.memory_bytes },
  { header: 'User', value: (p: ProcRow) => p.user_name },
  { header: 'Started', value: (p: ProcRow) => p.started_at },
  { header: 'Command', value: (p: ProcRow) => p.cmdline },
  { header: 'Updated', value: (p: ProcRow) => p.updated_at },
]

/* ── Tab ─────────────────────────────────────────────────────────── */

export function ProcessesTab({ server }: { server: ServerItem }) {
  const { data, isLoading, isError, error, refetch } = useProcesses(server.id)
  const items = useMemo(() => data?.items ?? [], [data])
  const memTotal = data?.mem_total_bytes || server.memory_total_bytes || 0

  const [q, setQ] = useState('')
  const [grouped, setGrouped] = useState(false)
  const [status, setStatus] = useState<StatusFilter>('all')
  const [sortBy, setSortBy] = useState<SortKey>('cpu')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [selected, setSelected] = useState<ProcRow | null>(null)

  /* Summary over the whole sample, independent of the table filters. */
  const summary = useMemo(() => {
    const running = items.filter((p) => p.running !== false)
    const cpu = running.reduce((s, p) => s + (p.cpu_pct || 0), 0)
    const mem = running.reduce((s, p) => s + (p.memory_bytes || 0), 0)
    const top = running.reduce<ServerProcess | null>(
      (best, p) => ((p.cpu_pct || 0) > (best?.cpu_pct || 0) ? p : best), null)
    const topMem = running.reduce<ServerProcess | null>(
      (best, p) => ((p.memory_bytes || 0) > (best?.memory_bytes || 0) ? p : best), null)
    const watched = new Set(items.filter((p) => p.watchlisted).map((p) => p.name))
    const missing = items.filter((p) => p.running === false && p.watchlisted)
    const names = new Set(running.map((p) => p.name))
    return { running: running.length, names: names.size, cpu, mem, top, topMem, watched: watched.size, missing }
  }, [items])

  const matched = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return items.filter((p) => {
      if (status === 'watched' && !p.watchlisted) return false
      if (status === 'missing' && !(p.watchlisted && p.running === false)) return false
      if (!needle) return true
      return p.name.toLowerCase().includes(needle) ||
        (p.user_name || '').toLowerCase().includes(needle) ||
        (p.cmdline || '').toLowerCase().includes(needle) ||
        String(p.pid).includes(needle)
    })
  }, [items, q, status])

  const rows = useMemo<ProcRow[]>(() => {
    const base = grouped ? groupByName(matched) : matched.map(toRow)
    const dir = sortDir === 'asc' ? 1 : -1
    return base.sort((a, b) => {
      switch (sortBy) {
        case 'name': return dir * a.name.localeCompare(b.name)
        case 'pid': return dir * (grouped ? a.count - b.count : (a.pid || 0) - (b.pid || 0))
        case 'user': return dir * (a.user_name || '').localeCompare(b.user_name || '') || a.name.localeCompare(b.name)
        case 'mem': return dir * (a.memory_bytes - b.memory_bytes)
        case 'updated': return dir * (a.updated_at || '').localeCompare(b.updated_at || '')
        default: return dir * (a.cpu_pct - b.cpu_pct)
      }
    })
  }, [matched, grouped, sortBy, sortDir])

  const totals = useMemo(() => ({
    cpu: matched.reduce((s, p) => s + (p.cpu_pct || 0), 0),
    mem: matched.reduce((s, p) => s + (p.memory_bytes || 0), 0),
  }), [matched])

  const newest = useMemo(
    () => items.reduce<string | null>(
      (max, p) => (p.updated_at && (!max || p.updated_at > max) ? p.updated_at : max), null),
    [items])

  const toggleSort = (col: SortKey) => {
    if (sortBy === col) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortBy(col); setSortDir(col === 'name' || col === 'user' ? 'asc' : 'desc') }
  }

  const memPct = (bytes: number) => (memTotal > 0 ? (bytes / memTotal) * 100 : null)
  const emptyMessage = telemetryEmptyMessage(server)
  const filtersActive = Boolean(q) || status !== 'all'

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)}
        </div>
        <Skeleton className="h-[420px] rounded-xl" />
      </div>
    )
  }

  if (isError) {
    return (
      <Panel title="Processes" icon={ListTree}>
        <QueryError error={error} onRetry={() => refetch()} />
      </Panel>
    )
  }

  const missingCount = summary.missing.length

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          icon={ListTree}
          label="Processes sampled"
          value={summary.running}
          sub={items.length >= SAMPLE_LIMIT
            ? `Top ${SAMPLE_LIMIT} by CPU · ${summary.names} distinct`
            : items.length ? `${summary.names} distinct names` : 'Not reported'}
        />
        <StatTile
          icon={Cpu}
          label="CPU (sampled)"
          value={fmtPct(summary.cpu)}
          tone={pctTone(summary.cpu)}
          pct={summary.cpu}
          sub={summary.top ? `Top: ${summary.top.name} ${fmtPct(summary.top.cpu_pct)}` : '—'}
        />
        <StatTile
          icon={MemoryStick}
          label="Memory (sampled)"
          value={formatBytes(summary.mem)}
          pct={memTotal > 0 ? memPct(summary.mem) : undefined}
          sub={memTotal > 0
            ? `${fmtPct(memPct(summary.mem))} of ${formatBytes(memTotal)}`
            : summary.topMem ? `Top: ${summary.topMem.name}` : '—'}
        />
        <StatTile
          icon={EyeOff}
          label="Watched, not running"
          value={missingCount}
          tone={missingCount > 0 ? 'warning' : 'default'}
          sub={summary.watched
            ? missingCount > 0
              ? summary.missing.slice(0, 3).map((p) => p.name).join(', ')
              : `All ${summary.watched} watched running`
            : 'No watchlist configured'}
          onClick={missingCount > 0 ? () => setStatus(status === 'missing' ? 'all' : 'missing') : undefined}
          active={status === 'missing'}
        />
      </div>

      <Panel
        flush
        icon={ListTree}
        title="Processes"
        subtitle={(
          <>
            {rows.length} {grouped ? 'groups' : 'processes'}
            {matched.length > 0 && <> · CPU {fmtPct(totals.cpu)} · memory {formatBytes(totals.mem)}</>}
            {newest && <> · updated {relativeTime(newest)}</>}
          </>
        )}
        actions={(
          <ExportCsvButton
            rows={rows}
            columns={PROCESSES_CSV}
            filename={`${server.display_name}-processes.csv`}
          />
        )}
      >
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border/70 px-4 py-2.5">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-8 pl-8 pr-8 text-[13px]"
              placeholder="Filter by name, user, PID or command…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            {q && (
              <button
                type="button"
                aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted hover:text-text"
                onClick={() => setQ('')}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <Segmented
            size="sm"
            value={status}
            onChange={setStatus}
            items={[
              { key: 'all', label: 'All' },
              { key: 'watched', label: 'Watchlist', icon: Eye, count: summary.watched },
              { key: 'missing', label: 'Not running', icon: EyeOff, count: missingCount, tone: 'warning' },
            ]}
          />
          <Segmented
            size="sm"
            value={grouped ? 'grouped' : 'instances'}
            onChange={(k) => setGrouped(k === 'grouped')}
            items={[
              { key: 'instances', label: 'Instances', icon: ListTree },
              { key: 'grouped', label: 'Group by name', icon: Layers },
            ]}
          />
        </div>

        {/* The API returns the top 200 by CPU. Sorting by memory therefore
            reorders that CPU-selected slice — a high-memory, idle process is
            not in it at all. Say so rather than implying a full process list. */}
        {items.length >= SAMPLE_LIMIT && (
          <div className="flex items-start gap-2 border-b border-border/70 bg-warning/[0.05] px-4 py-2 text-xs text-muted">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <span>
              Showing the {SAMPLE_LIMIT} most CPU-intensive processes. Sorting by memory reorders
              this sample, so a low-CPU process using a lot of memory may not appear.
            </span>
          </div>
        )}

        {rows.length === 0 ? (
          items.length === 0 ? (
            <EmptyState icon={ListTree} title="No process data" hint={emptyMessage} />
          ) : (
            <EmptyState
              icon={Search}
              title="No processes match these filters"
              hint="Clear the search or status filter to see the full sample."
              action={filtersActive ? (
                <Button size="sm" variant="outline" className="mt-1" onClick={() => { setQ(''); setStatus('all') }}>
                  Clear filters
                </Button>
              ) : undefined}
            />
          )
        ) : (
          <div className="max-h-[640px] overflow-auto">
            <table className="w-full min-w-[880px] border-separate border-spacing-0 text-[13px]">
              <thead>
                <tr className="text-left text-[11px] font-medium uppercase tracking-wide text-muted">
                  <HeadCell className={cn('pl-4', sortableTh)} onClick={() => toggleSort('name')}>
                    Process{sortIndicator(sortBy === 'name', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-20 text-right', sortableTh)} onClick={() => toggleSort('pid')}>
                    {grouped ? 'Count' : 'PID'}{sortIndicator(sortBy === 'pid', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-40', sortableTh)} onClick={() => toggleSort('user')}>
                    User{sortIndicator(sortBy === 'user', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-44', sortableTh)} onClick={() => toggleSort('cpu')}>
                    CPU{sortIndicator(sortBy === 'cpu', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-52', sortableTh)} onClick={() => toggleSort('mem')}>
                    Memory{sortIndicator(sortBy === 'mem', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-24', sortableTh)} onClick={() => toggleSort('updated')}>
                    Updated{sortIndicator(sortBy === 'updated', sortDir)}
                  </HeadCell>
                  <HeadCell className="w-10 pr-4"><span className="sr-only">History</span></HeadCell>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => {
                  const mp = memPct(p.memory_bytes)
                  const cpuTone = p.running ? pctTone(p.cpu_pct, PROC_WARN, PROC_CRIT) : 'muted'
                  const memTone = p.running ? pctTone(mp, PROC_WARN, PROC_CRIT) : 'muted'
                  return (
                    <tr
                      key={p.key}
                      onClick={() => setSelected(p)}
                      className={cn(
                        'group cursor-pointer transition-colors hover:bg-surface2/40',
                        !p.running && 'bg-warning/[0.04]',
                      )}
                    >
                      <BodyCell className="max-w-0 pl-4">
                        <div className="flex min-w-0 items-center gap-2">
                          <button
                            type="button"
                            className="truncate text-left font-medium text-text group-hover:text-primary focus-visible:underline focus-visible:outline-none"
                            title={p.cmdline || p.name}
                            onClick={(e) => { e.stopPropagation(); setSelected(p) }}
                          >
                            {p.name}
                          </button>
                          {!p.running && <Pill tone="warning">Not running</Pill>}
                          {p.watchlisted && (
                            <span
                              title="On the monitoring profile's process watchlist"
                              className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-1.5 py-px text-[10px] font-medium text-muted"
                            >
                              <Eye className="h-3 w-3" /> Watched
                            </span>
                          )}
                        </div>
                      </BodyCell>
                      <BodyCell className="text-right tabular-nums text-muted">
                        {grouped ? `×${p.count}` : p.pid ?? '—'}
                      </BodyCell>
                      <BodyCell className="max-w-0 text-xs text-muted">
                        <span className="block truncate" title={p.user_name || undefined}>{p.user_name || '—'}</span>
                      </BodyCell>
                      <BodyCell>
                        <div className="flex items-center gap-2.5">
                          <Meter pct={p.running ? p.cpu_pct : null} warn={PROC_WARN} crit={PROC_CRIT} className="flex-1" />
                          <span className={cn('w-12 shrink-0 text-right text-xs font-medium tabular-nums', TONE_TEXT[cpuTone])}>
                            {p.running ? fmtPct(p.cpu_pct) : '—'}
                          </span>
                        </div>
                      </BodyCell>
                      <BodyCell>
                        <div className="flex items-center gap-2.5">
                          <Meter pct={p.running ? mp : null} warn={PROC_WARN} crit={PROC_CRIT} className="flex-1" />
                          <span
                            className="w-[4.5rem] shrink-0 text-right text-xs tabular-nums text-text2"
                            title={mp == null ? undefined : `${fmtPct(mp)} of ${formatBytes(memTotal)}`}
                          >
                            {p.running ? formatBytes(p.memory_bytes) : '—'}
                          </span>
                          <span className={cn('w-11 shrink-0 text-right text-[11px] tabular-nums', TONE_TEXT[memTone === 'default' ? 'muted' : memTone])}>
                            {p.running && mp != null ? fmtPct(mp) : ''}
                          </span>
                        </div>
                      </BodyCell>
                      <BodyCell
                        className="whitespace-nowrap text-xs text-muted"
                        title={p.updated_at ? new Date(p.updated_at).toLocaleString() : undefined}
                      >
                        {relativeTime(p.updated_at)}
                      </BodyCell>
                      <BodyCell className="pr-4 text-right">
                        <ChevronRight className="ml-auto h-4 w-4 text-muted/50 transition-colors group-hover:text-primary" />
                      </BodyCell>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <ProcessDetailDialog
        serverId={server.id}
        row={selected}
        grouped={grouped}
        memTotal={memTotal}
        onClose={() => setSelected(null)}
      />
    </div>
  )
}

/* ── Table cells ─────────────────────────────────────────────────── */

function HeadCell({ className, children, onClick }: {
  className?: string
  children: ReactNode
  onClick?: () => void
}) {
  return (
    <th
      onClick={onClick}
      className={cn(
        'sticky top-0 z-10 h-9 border-b border-border bg-surface px-3 align-middle font-medium',
        className,
      )}
    >
      {children}
    </th>
  )
}

function BodyCell({ className, children, title }: {
  className?: string
  children: ReactNode
  title?: string
}) {
  return (
    <td title={title} className={cn('h-10 border-b border-border/60 px-3 py-1.5 align-middle', className)}>
      {children}
    </td>
  )
}

/* ── Detail + history ────────────────────────────────────────────── */

function ProcessDetailDialog({ serverId, row, grouped, memTotal, onClose }: {
  serverId: string
  row: ProcRow | null
  grouped: boolean
  memTotal: number
  onClose: () => void
}) {
  const mp = row && memTotal > 0 ? (row.memory_bytes / memTotal) * 100 : null
  return (
    <Dialog open={Boolean(row)} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto p-0">
        {row && (
          <>
            <DialogHeader className="border-b border-border/70 px-5 pb-3 pt-5">
              <DialogTitle className="flex flex-wrap items-center gap-2 pr-8 text-base">
                <span className="truncate">{row.name}</span>
                <Pill tone={row.running ? 'success' : 'warning'}>{stateLabel(row.state)}</Pill>
                {row.watchlisted && <Pill tone="info">Watched</Pill>}
              </DialogTitle>
              <DialogDescription className="text-xs">
                {grouped
                  ? `${row.count} instance${row.count === 1 ? '' : 's'} grouped by name`
                  : `PID ${row.pid ?? '—'}`}
                {row.user_name && ` · ${row.user_name}`}
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 px-5 pb-5">
              <KeyValues
                columns={4}
                items={[
                  ['CPU', row.running ? fmtPct(row.cpu_pct) : '—'],
                  ['Memory', row.running
                    ? <>{formatBytes(row.memory_bytes)}{mp != null && <span className="text-muted"> · {fmtPct(mp)}</span>}</>
                    : '—'],
                  ['Started', row.started_at ? new Date(row.started_at).toLocaleString() : 'unknown'],
                  ['Updated', row.updated_at ? relativeTime(row.updated_at) : '—'],
                ]}
              />
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Command line</div>
                <div className="mt-1 max-h-24 overflow-auto rounded-md border border-border bg-surface2/40 px-2.5 py-1.5 font-mono text-xs text-text2 [overflow-wrap:anywhere]">
                  {row.cmdline || 'not reported'}
                </div>
              </div>
              <ProcessHistory serverId={serverId} name={row.name} />
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ProcessHistory({ serverId, name }: { serverId: string; name: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'proc-history', name],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/processes/history`, { params: { name } })).data,
    refetchInterval: 60_000,
    enabled: Boolean(serverId && name),
  })

  const rows = useMemo(
    () => seriesRows(data, { cpu_pct: 'cpu', memory_bytes: 'mem' }),
    [data],
  )
  const tick = useMemo(() => timeAxisTickFormatter(6), [])
  const cpuStats = stats(rows.map((r) => r.cpu))
  const memStats = stats(rows.map((r) => r.mem))
  const cpuFmt = (v: number) => `${v < 10 ? v.toFixed(1) : v.toFixed(0)}%`
  const memFmt = (v: number) => formatBytes(v)

  if (isError) {
    return (
      <Panel title="History" subtitle="Last 6 hours" icon={History}>
        <QueryError error={error} onRetry={() => refetch()} />
      </Panel>
    )
  }

  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Panel title="CPU" subtitle={`${name} · last 6 hours`} icon={Cpu}>
        {isLoading ? (
          <Skeleton className="h-[236px] w-full" />
        ) : !cpuStats ? (
          <EmptyState compact icon={History} title="No CPU history" hint="Nothing recorded for this process in the last 6 hours." />
        ) : (
          <>
            <ChartStats
              items={[
                { label: 'Current', value: fmtPct(cpuStats.current), tone: pctTone(cpuStats.current, PROC_WARN, PROC_CRIT) },
                { label: 'Average', value: fmtPct(cpuStats.avg) },
                { label: 'P95', value: fmtPct(cpuStats.p95) },
                { label: 'Peak', value: fmtPct(cpuStats.peak), tone: pctTone(cpuStats.peak, PROC_WARN, PROC_CRIT) },
              ]}
            />
            <TimeSeries
              data={rows}
              series={[{ key: 'cpu', label: 'CPU', color: C.sky }]}
              height={180}
              yFormat={cpuFmt}
              tickFormat={tick}
            />
          </>
        )}
      </Panel>
      <Panel title="Memory" subtitle="All instances · last 6 hours" icon={MemoryStick}>
        {isLoading ? (
          <Skeleton className="h-[236px] w-full" />
        ) : !memStats ? (
          <EmptyState compact icon={History} title="No memory history" hint="Nothing recorded for this process in the last 6 hours." />
        ) : (
          <>
            <ChartStats
              items={[
                { label: 'Current', value: formatBytes(memStats.current) },
                { label: 'Average', value: formatBytes(memStats.avg) },
                { label: 'P95', value: formatBytes(memStats.p95) },
                { label: 'Peak', value: formatBytes(memStats.peak) },
              ]}
            />
            <TimeSeries
              data={rows}
              series={[{ key: 'mem', label: 'Memory', color: C.blue }]}
              height={180}
              yFormat={memFmt}
              tickFormat={tick}
            />
          </>
        )}
      </Panel>
    </div>
  )
}
