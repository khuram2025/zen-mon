import { regionalDate } from '@/lib/regionalTime'
import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Layers, ScrollText } from 'lucide-react'
import { api } from '@/lib/api'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { QueryError } from '@/components/servers/tables'
import type { ServerItem } from '@/types/servers'
import { EmptyState, Panel, Pill, StatTile, type Tone } from '../ui'

type Group = {
  group_key: string; log_name: string; provider: string; event_id: number; level: string;
  component: string; count: number; first_seen: string; last_seen: string; message: string; message_truncated: boolean
}
type Occurrence = {
  event_uid: string; timestamp: string; record_id: number; message: string;
  fields: Record<string, string>; message_truncated: boolean
}
type GroupsResponse = {
  items: Group[]; has_more: boolean; total_events: number; total_groups: number;
  levels: Record<string, number>; channels: string[]; details_capable: boolean; agent_version: string | null;
  from: string; to: string;
  collection: null | { checked_at: string; received_at: string; gap_at: string | null;
    status: { interval_s?: number; bytes: number; max_bytes: number; max_events: number;
      queries: { channel: string; backlog: boolean; gap: boolean; error?: string }[] } }
}
const levels = ['critical', 'error', 'warning', 'information', 'audit', 'verbose']
const tone = (level: string): Tone => level === 'critical' || level === 'error' ? 'danger' : level === 'warning' ? 'warning' : 'info'
const time = (s: string) => regionalDate(new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s}Z`), 'datetime')
const pageSize = 50

export function GroupedEventsTab({ server }: { server: ServerItem }) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [level, setLevel] = useState('all')
  const [channel, setChannel] = useState('all')
  const [page, setPage] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  useEffect(() => { const timer = setTimeout(() => { setQuery(search); setPage(0) }, 300); return () => clearTimeout(timer) }, [search])
  const windowParams = isCustom ? { from: range.fromISO, to: range.toISO } : { hours: range.hours }
  const windowKey = isCustom ? `${range.fromISO}/${range.toISO}` : `last-${range.hours}h`
  useEffect(() => { setPage(0); setExpanded(null) }, [windowKey, server.id, level, channel, query])
  const result = useQuery<GroupsResponse>({
    queryKey: ['server-event-groups', server.id, windowKey, query, level, channel, page],
    queryFn: async () => (await api.get(`/servers/${server.id}/event-groups`, { params: {
      ...windowParams, search: query, ...(level !== 'all' ? { level } : {}), ...(channel !== 'all' ? { channel } : {}), limit: pageSize, offset: page * pageSize,
    } })).data,
    refetchInterval: isCustom ? false : 60_000,
  })
  const data = result.data
  const collection = data?.collection
  const statuses = collection?.status.queries ?? []
  const backlog = statuses.some(q => q.backlog)
  const errors = statuses.filter(q => q.error)
  const stale = collection && Date.now() - Date.parse(collection.checked_at) > Math.max(300, 3 * (collection.status.interval_s || 60)) * 1000
  const channels = [...new Set(['System', 'Application', 'Security', channel, ...(data?.channels || []), ...statuses.map(q => q.channel)])].filter(c => c !== 'all')
  const changePage = (next: number) => { setPage(next); setExpanded(null) }
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-xs text-muted">Unique Windows events · {range.label} · 14 days of detailed history</p>
      <TimeRangePicker rangeIdx={rangeIdx} isCustom={isCustom} customFrom={range.fromISO} customTo={range.toISO} onPreset={setPreset} onCustom={setCustom} />
    </div>
    {result.isError && <QueryError error={result.error} onRetry={() => result.refetch()} />}
    {data && !data.details_capable && <div className="rounded-lg border border-border bg-surface2/40 p-4 text-sm">
      {server.os_type === 'windows' ? <>Update this server to agent 1.14.4 or later for grouped event details. Current version: {data.agent_version || 'unknown'}. Existing history is available under Legacy summaries.</> : <>Windows event details apply to Windows servers. OS boot changes also feed restart alerts for agent-monitored servers.</>}
    </div>}
    {data?.details_capable && !collection && <div className="rounded-lg border p-3 text-sm text-muted">Waiting for the first detailed event collection. Check the server’s <Link className="text-primary" to="/agent-policies">agent policy</Link> if collection is disabled.</div>}
    {collection && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border p-3 text-xs">
      <Pill tone={errors.length || stale ? 'warning' : backlog ? 'info' : 'success'}>{stale ? 'Collection stale' : errors.length ? 'Collection incomplete' : backlog ? 'Catching up' : 'Collection current'}</Pill>
      <span>Checked {time(collection.checked_at)}</span>
      <span>{Math.round((collection.status.bytes || 0) / 1024)} KB / {Math.round((collection.status.max_bytes || 0) / 1024)} KB per batch</span>
      {backlog && <span>Batch limit reached; remaining events stay in the Windows log for the next collection.</span>}
      {collection.gap_at && <span className="text-warning">A cleared or rolled log caused a coverage gap at {time(collection.gap_at)}.</span>}
      {errors.length > 0 && <span className="text-warning">{[...new Set(errors.map(q => `${q.channel}: ${q.error}`))].join(' · ')}</span>}
    </div>}
    <div className="grid gap-3 sm:grid-cols-3">
      <StatTile icon={ScrollText} label="Occurrences" value={data?.total_events.toLocaleString() ?? '—'} sub="Unique events in the filtered window" />
      <StatTile icon={Layers} label="Event groups" value={data?.total_groups.toLocaleString() ?? '—'} sub="Repeated events grouped by source and affected component" />
      <StatTile icon={AlertTriangle} label="Errors + critical" value={data ? ((data.levels.error || 0) + (data.levels.critical || 0)).toLocaleString() : '—'} sub="Notifications follow configured alert rules" />
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <Input className="max-w-sm" aria-label="Search event details" placeholder="Search source, event ID, component or message…" value={search} onChange={e => setSearch(e.target.value)} />
      <Select value={level} onValueChange={setLevel}><SelectTrigger className="w-40" aria-label="Event severity"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All levels</SelectItem>{levels.map(l => <SelectItem key={l} value={l}>{l}</SelectItem>)}</SelectContent></Select>
      <Select value={channel} onValueChange={setChannel}><SelectTrigger className="w-52" aria-label="Event channel"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All channels</SelectItem>{channels.map(c => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent></Select>
    </div>
    <Panel icon={Layers} title="Grouped event details" subtitle="Expand a group to inspect messages and individual occurrences" flush>
      <div className="overflow-x-auto"><Table><THead><Tr><Th>Source / Event ID</Th><Th>Related to</Th><Th>Level</Th><Th>Occurrences</Th><Th>First / last seen</Th><Th>Details</Th></Tr></THead><TBody>
        {data?.items.map(group => <Fragment key={group.group_key}>
          <Tr><Td><div className="font-medium">{group.provider} · {group.event_id}</div><div className="text-xs text-muted">{group.log_name}</div></Td>
            <Td><div className="max-w-sm break-words text-sm">{group.component || 'Component not supplied'}</div><div className="max-w-sm truncate text-xs text-muted" title={group.message}>{group.message || 'Message unavailable; inspect event fields'}</div></Td>
            <Td><Pill tone={tone(group.level)}>{group.level}</Pill></Td><Td className="font-semibold tabular-nums">{group.count.toLocaleString()}</Td>
            <Td className="whitespace-nowrap text-xs">{time(group.first_seen)}<br />{time(group.last_seen)}</Td>
            <Td><button className="text-sm text-primary" aria-expanded={expanded === group.group_key} onClick={() => setExpanded(expanded === group.group_key ? null : group.group_key)}>{expanded === group.group_key ? 'Hide' : 'View details'}</button></Td></Tr>
          {expanded === group.group_key && <Tr><Td colSpan={6}><Occurrences serverId={server.id} group={group.group_key} from={data.from} to={data.to} /></Td></Tr>}
        </Fragment>)}
      </TBody></Table></div>
      {result.isLoading && <p className="p-6 text-sm text-muted">Loading events…</p>}
      {!result.isLoading && !result.isError && !data?.items.length && <EmptyState title="No matching detailed events" hint="Collection policies select useful events. An empty result does not prove the server is healthy; check collection status above." />}
      <div className="flex items-center justify-between border-t border-border p-3 text-sm"><span>Page {page + 1}</span><div className="flex gap-4"><button disabled={page === 0 || result.isFetching} className="text-primary disabled:opacity-40" onClick={() => changePage(page - 1)}>Previous</button><button disabled={!data?.has_more || result.isFetching || page >= 2000} className="text-primary disabled:opacity-40" onClick={() => changePage(page + 1)}>Next</button></div></div>
    </Panel>
  </div>
}

function Occurrences({ serverId, group, from, to }: { serverId: string; group: string; from: string; to: string }) {
  const [page, setPage] = useState(0)
  const result = useQuery<{ items: Occurrence[]; has_more: boolean }>({
    queryKey: ['event-occurrences', serverId, group, from, to, page],
    queryFn: async () => (await api.get(`/servers/${serverId}/event-groups/${group}`, { params: { from, to, limit: 25, offset: page * 25 } })).data,
  })
  return <div className="space-y-3 p-2">
    {result.isLoading && <p>Loading occurrences…</p>}
    {result.isError && <QueryError error={result.error} onRetry={() => result.refetch()} />}
    {result.data?.items.map(event => <div key={event.event_uid} className="rounded-md border border-border bg-surface2/30 p-3">
      <p className="mb-2 text-xs text-muted">{time(event.timestamp)} · Record {event.record_id}{event.message_truncated ? ' · Message shortened by collection policy' : ''}</p>
      <p className="whitespace-pre-wrap break-words text-sm">{event.message || 'Provider message unavailable'}</p>
      {Object.keys(event.fields).length > 0 && <details className="mt-2 text-xs"><summary className="cursor-pointer text-primary">Event fields</summary><dl className="mt-2 grid grid-cols-[minmax(100px,auto)_1fr] gap-x-3 gap-y-1">{Object.entries(event.fields).map(([k,v]) => <Fragment key={k}><dt className="text-muted">{k}</dt><dd className="break-all">{v}</dd></Fragment>)}</dl></details>}
    </div>)}
    <div className="flex gap-4 text-sm"><button disabled={page === 0} className="text-primary disabled:opacity-40" onClick={() => setPage(page - 1)}>Previous occurrences</button><button disabled={!result.data?.has_more || page >= 4000} className="text-primary disabled:opacity-40" onClick={() => setPage(page + 1)}>More occurrences</button></div>
  </div>
}
