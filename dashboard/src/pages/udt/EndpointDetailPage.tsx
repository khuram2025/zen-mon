import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Ban, Check, Eye, EyeOff, ShieldCheck } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage } from '@/lib/utils'
import { udtApi } from './api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { KbLink } from '@/components/udt/KbLink'
import type { EndpointLocation } from './types'
import {
  AuthBadge, EndpointTypeIcon, EventBadge, TypeSourceBadge, durationBetween,
  durationSince, endpointTypeMeta, fmtDate, portLabel, relTime,
} from './helpers'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-0.5 text-sm">{children}</div>
    </div>
  )
}

function TypeEditor({ value, source, onChange }: {
  value: string; source?: 'auto' | 'rule' | 'manual'; onChange: (t: string) => void
}) {
  const types = useQuery({ queryKey: ['udt', 'types'], queryFn: () => udtApi.types() })
  const options = types.data?.data || []
  const known = options.some((t) => t.type === value)
  return (
    <span className="inline-flex items-center gap-1.5">
      <EndpointTypeIcon type={value} className="h-3.5 w-3.5" />
      <Select value={value} onValueChange={(v) => onChange(v)}>
        <SelectTrigger className="h-7 w-auto min-w-[130px] text-sm"><SelectValue /></SelectTrigger>
        <SelectContent>
          {!known && <SelectItem value={value}>{endpointTypeMeta(value).label}</SelectItem>}
          {options.map((t) => (
            <SelectItem key={t.type} value={t.type}>{endpointTypeMeta(t.type).label}</SelectItem>
          ))}
          <SelectItem value="auto">Auto (reclassify)</SelectItem>
        </SelectContent>
      </Select>
      <TypeSourceBadge source={source} />
    </span>
  )
}

function TimelineEntry({ loc }: { loc: EndpointLocation }) {
  return (
    <li className="relative pb-4 pl-5 last:pb-1">
      {/* rail + dot */}
      <span className="absolute left-[3px] top-[18px] bottom-0 w-px bg-border" aria-hidden />
      <span className={`absolute left-0 top-[6px] h-[7px] w-[7px] rounded-full ring-2 ${
        loc.active ? 'bg-success ring-success/25' : 'bg-muted/50 ring-transparent'
      }`} aria-hidden />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Link to={`/devices/${loc.device_id}`} className="text-sm font-medium text-primary hover:underline">
          {loc.switch}
        </Link>
        <span className="rounded bg-surface2 px-1.5 py-0.5 font-mono text-[11px]">{portLabel(loc.if_name, loc.if_index)}</span>
        {loc.vlan_id != null && <span className="text-[11px] text-muted">VLAN {loc.vlan_id}</span>}
        {loc.is_direct
          ? <Badge variant="success">direct</Badge>
          : <Badge variant="outline" title="MAC learned through another switch's uplink">via uplink</Badge>}
        {loc.active && <Badge variant="info">connected</Badge>}
      </div>
      {loc.if_alias && <div className="mt-0.5 text-[11px] text-muted">{loc.if_alias}</div>}
      <div className="mt-0.5 text-[11px] text-muted tabular-nums">
        {fmtDate(loc.first_seen)} → {loc.active ? 'now' : fmtDate(loc.closed_at || loc.last_seen)}
        <span className="ml-2 rounded bg-surface2 px-1.5 py-px">{durationBetween(loc.first_seen, loc.active ? null : (loc.closed_at || loc.last_seen))}</span>
      </div>
    </li>
  )
}

function ConnectionHistoryCard({ locations }: { locations: EndpointLocation[] }) {
  const [showIndirect, setShowIndirect] = useState(false)
  const byRecency = (a: EndpointLocation, b: EndpointLocation) =>
    Number(b.active) - Number(a.active) || new Date(b.last_seen).getTime() - new Date(a.last_seen).getTime()
  const direct = locations.filter((l) => l.is_direct).sort(byRecency)
  const indirect = locations.filter((l) => !l.is_direct).sort(byRecency)
  // Without any direct sighting (endpoint only ever seen through uplinks)
  // the indirect rows ARE the history — show them unconditionally.
  const shown = direct.length === 0 ? indirect : (showIndirect ? [...direct, ...indirect].sort(byRecency) : direct)
  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold">Connection history</h3>
          {direct.length > 0 && indirect.length > 0 && (
            <button
              onClick={() => setShowIndirect((v) => !v)}
              className="text-xs text-muted transition-colors hover:text-text"
            >
              {showIndirect ? 'Hide' : 'Show'} {indirect.length} uplink sighting{indirect.length === 1 ? '' : 's'}
            </button>
          )}
        </div>
        {shown.length === 0 ? (
          <div className="p-6 text-center text-sm text-muted">No connection history yet.</div>
        ) : (
          <ul className="max-h-[420px] overflow-y-auto p-4">
            {shown.map((l) => <TimelineEntry key={l.id} loc={l} />)}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

const LOGON_TYPES: Record<number, string> = {
  2: 'Interactive', 3: 'Network', 4: 'Batch', 5: 'Service',
  7: 'Unlock', 8: 'NetworkClear', 10: 'RemoteInteractive', 11: 'CachedInteractive',
}

export function EndpointDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['udt', 'endpoint', id],
    queryFn: () => udtApi.endpoint(id),
    refetchInterval: 20_000,
    enabled: !!id,
  })

  const mutate = useMutation({
    mutationFn: (body: Record<string, any>) => udtApi.updateEndpoint(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['udt', 'endpoint', id] })
      qc.invalidateQueries({ queryKey: ['udt', 'summary'] })
      toast.success('Endpoint updated')
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const e = data.endpoint
  const meta = endpointTypeMeta(e.endpoint_type)
  const activeLoc = data.locations.find((l) => l.active)

  return (
    <div className="space-y-4">
      <button onClick={() => navigate(-1)} className="inline-flex items-center gap-1 text-xs text-muted hover:text-text">
        <ArrowLeft className="h-3.5 w-3.5" /> Back
      </button>

      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <EndpointTypeIcon type={e.endpoint_type} className="h-6 w-6" />
          </div>
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              {e.hostname || meta.label}
              <AuthBadge authorized={e.authorized} watched={e.is_watched} randomized={e.is_randomized} />
              <KbLink article="endpoints" label="Endpoint detail documentation" />
            </h1>
            <p className="mt-0.5 font-mono text-xs text-muted">{e.mac}{e.ip ? ` · ${e.ip}` : ''}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant={e.is_watched ? 'secondary' : 'outline'}
            onClick={() => mutate.mutate({ is_watched: !e.is_watched })}>
            {e.is_watched ? <EyeOff className="mr-1 h-4 w-4" /> : <Eye className="mr-1 h-4 w-4" />}
            {e.is_watched ? 'Unwatch' : 'Watch'}
          </Button>
          {e.authorized === false ? (
            <Button size="sm" variant="outline"
              onClick={() => mutate.mutate({ authorized: true })}>
              <ShieldCheck className="mr-1 h-4 w-4" /> Mark allowed
            </Button>
          ) : (
            <Button size="sm" variant="outline"
              onClick={() => mutate.mutate({ authorized: false })}>
              <Ban className="mr-1 h-4 w-4" /> Flag rogue
            </Button>
          )}
          <Button size="sm" variant={e.ignored ? 'secondary' : 'ghost'}
            onClick={() => mutate.mutate({ ignored: !e.ignored })}>
            {e.ignored ? 'Un-ignore' : 'Ignore'}
          </Button>
        </div>
      </div>

      {/* Identity card */}
      <Card>
        <CardContent className="grid grid-cols-2 gap-4 p-4 sm:grid-cols-3 lg:grid-cols-6">
          <Field label="Type">
            <TypeEditor value={e.endpoint_type} source={e.type_source}
              onChange={(t) => mutate.mutate({ endpoint_type: t })} />
          </Field>
          <Field label="Vendor">{e.vendor || '—'}</Field>
          <Field label="Current location">
            {activeLoc ? (
              <Link to={`/devices/${activeLoc.device_id}`} className="text-primary hover:underline">
                {activeLoc.switch} · {portLabel(activeLoc.if_name, activeLoc.if_index)}
              </Link>
            ) : <span className="text-muted">Not currently connected</span>}
          </Field>
          <Field label="VLAN">{activeLoc?.vlan_id ?? '—'}</Field>
          <Field label="Last user">{e.user_name ? `${e.user_domain ? e.user_domain + '\\' : ''}${e.user_name}` : '—'}</Field>
          <Field label="Connected for">{activeLoc ? durationSince(activeLoc.first_seen) : '—'}</Field>
          <Field label="First seen">{fmtDate(e.first_seen)}</Field>
          <Field label="Last seen">{relTime(e.last_seen)}</Field>
          {e.managed_hostname && <Field label="Managed device"><Link to={`/devices/${e.device_id}`} className="text-primary hover:underline">{e.managed_hostname}</Link></Field>}
          {e.is_randomized && <Field label="MAC"><Badge variant="info">Randomized / private</Badge></Field>}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Connection history */}
        <ConnectionHistoryCard locations={data.locations} />

        {/* IP history */}
        <Card>
          <CardContent className="p-0">
            <h3 className="border-b border-border px-4 py-3 text-sm font-semibold">IP address history</h3>
            {data.ip_history.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted">No IP bindings observed (needs ARP from an L3 device).</div>
            ) : (
              <Table>
                <THead className="bg-surface2/40">
                  <Tr><Th>IP</Th><Th>Source</Th><Th>State</Th><Th className="text-right">First</Th><Th className="text-right">Last</Th></Tr>
                </THead>
                <TBody>
                  {data.ip_history.map((ip, i) => (
                    <Tr key={i}>
                      <Td className="font-mono text-xs tabular-nums">{ip.ip}</Td>
                      <Td className="text-xs uppercase text-muted">{ip.source}</Td>
                      <Td>{ip.active ? <Badge variant="success">active</Badge> : <Badge variant="outline">past</Badge>}</Td>
                      <Td className="text-right text-xs text-muted">{relTime(ip.first_seen)}</Td>
                      <Td className="text-right text-xs text-muted">{ip.active ? 'now' : relTime(ip.last_seen)}</Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* User logins */}
        <Card>
          <CardContent className="p-0">
            <h3 className="border-b border-border px-4 py-3 text-sm font-semibold">User logins</h3>
            {data.logins.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted">No AD logins correlated. Add a domain controller under Settings.</div>
            ) : (
              <Table>
                <THead className="bg-surface2/40">
                  <Tr><Th>User</Th><Th>Type</Th><Th>Event</Th><Th className="text-right">When</Th></Tr>
                </THead>
                <TBody>
                  {data.logins.map((l, i) => (
                    <Tr key={i}>
                      <Td className="text-xs font-medium">{l.user_domain ? `${l.user_domain}\\` : ''}{l.user_name}</Td>
                      <Td className="text-xs text-muted">{l.logon_type ? (LOGON_TYPES[l.logon_type] || l.logon_type) : '—'}</Td>
                      <Td className="text-xs tabular-nums text-muted">{l.event_id ?? '—'}</Td>
                      <Td className="text-right text-xs text-muted">{relTime(l.event_time)}</Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Events */}
        <Card>
          <CardContent className="p-0">
            <h3 className="border-b border-border px-4 py-3 text-sm font-semibold">Activity</h3>
            {data.events.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted">No activity recorded.</div>
            ) : (
              <ul className="divide-y divide-border">
                {data.events.map((ev, i) => (
                  <li key={i} className="flex items-center justify-between px-4 py-2.5 text-xs">
                    <span className="flex items-center gap-2">
                      <EventBadge type={ev.event_type} />
                      {ev.switch && <span className="text-muted">{ev.switch}{ev.if_index ? ` · if ${ev.if_index}` : ''}</span>}
                    </span>
                    <span className="text-muted">{relTime(ev.created_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
