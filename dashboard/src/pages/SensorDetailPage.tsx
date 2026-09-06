import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, RefreshCw, Download, Server, Activity, Radio, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { SensorDetailDialog, TokenDialog, type Sensor, type TokenInfo } from '@/components/SensorsCard'

type Observation = { state: string; last_result_at: string | null; latency_ms: number | null; availability_pct: number | null; samples: number }
type Target = { id: string; name: string; address: string | null; check_type: string | null; assignment_source: string; checks: Record<string, Observation> }
type Overview = { release: {version: string | null; available: boolean}; sensor: Sensor; controller_url: string; devices: Target[]; services: Target[]; measurements_available: boolean; observed_at: string }
type Command = { id: string; verb: string; status: string; result: string | null; created_at: string; completed_at: string | null; expires_at: string; payload: { version?: string } }
type Event = { id: string; kind: string; ts: string; detail: { reason?: string } }
const labels: Record<string, string> = { online: 'Connected', offline: 'Disconnected', degraded: 'Degraded', disabled: 'Disabled', pending: 'Awaiting authorization', up: 'Up', down: 'Down', no_data: 'No recent data', probe_offline: 'Sensor offline', probe_disabled: 'Sensor disabled', probe_pending: 'Awaiting authorization', update_required: 'Sensor update required' }
function Status({ state: rawState, command = false }: { state: string; command?: boolean }) {
  const state = command && rawState === 'pending' ? 'queued' : rawState
  return <Badge variant={['online','up','succeeded'].includes(state) ? 'success' : ['offline','down','failed','expired'].includes(state) ? 'danger' : ['degraded','pending','queued','delivered','update_required'].includes(state) ? 'warning' : 'outline'}>{labels[state] || state.replace(/_/g, ' ')}</Badge>
}
function uptime(seconds: number | null) {
  if (seconds == null) return 'Not reported'
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60)
  return `${days ? `${days}d ` : ''}${hours}h ${minutes}m`
}
function newer(published: string | undefined, current: string | null) {
  if (!published || !current) return false
  const a = (published.match(/\d+/g) || []).slice(0,3).map(Number), b = (current.match(/\d+/g) || []).slice(0,3).map(Number)
  for (let i=0;i<3;i++) { if ((a[i]||0)!==(b[i]||0)) return (a[i]||0)>(b[i]||0) }
  return false
}
function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="min-w-0"><dt className="text-xs text-muted">{label}</dt><dd className="mt-1 break-words text-sm font-medium">{children || 'Not reported'}</dd></div>
}
function TargetTable({ title, items, kind, search }: { title: string; items: Target[]; kind: 'devices' | 'services'; search: string }) {
  const [limit, setLimit] = useState(50)
  const filtered = items.filter(item => `${item.name} ${item.address || ''} ${item.check_type || ''}`.toLowerCase().includes(search.toLowerCase()))
  return <Card><CardHeader><CardTitle>{title} <span className="ml-1 text-sm font-normal text-muted">{items.length}</span></CardTitle></CardHeader><CardContent>
    {!items.length ? <p className="py-5 text-sm text-muted">No {title.toLowerCase()} are assigned to this sensor. Use Manage sensor to add monitoring assignments.</p> : !filtered.length ? <p className="py-5 text-sm text-muted">No matching {title.toLowerCase()}.</p> : <>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="border-b border-border text-xs text-muted"><tr><th className="pb-3 pr-4">{kind === 'devices' ? 'Device / IP address' : 'Service / check type'}</th><th className="pb-3 pr-4">Assignment</th><th className="pb-3 pr-4">Result from this sensor</th><th className="pb-3">Observed availability · 24h</th></tr></thead><tbody className="divide-y divide-border/60">
        {filtered.slice(0,limit).map(item => <tr key={item.id}><td className="py-3 pr-4 align-top"><Link className="font-medium text-primary hover:underline" to={`/${kind}/${item.id}`}>{item.name}</Link><div className="mt-1 text-xs text-muted">{item.address || item.check_type?.toUpperCase()}</div></td><td className="py-3 pr-4 align-top text-xs text-muted">{item.assignment_source}</td><td className="py-3 pr-4 align-top"><div className="space-y-2">{Object.entries(item.checks).map(([check,result]) => <div key={check}><div className="flex flex-wrap items-center gap-2"><span className="min-w-12 text-xs font-medium">{check === 'service' ? item.check_type?.toUpperCase() : check.toUpperCase()}</span><Status state={result.state} /></div>{result.state !== 'disabled' && <p className="mt-1 text-xs text-muted">{result.last_result_at ? relativeTime(result.last_result_at) : 'Awaiting first result'}{result.latency_ms != null ? ` · ${result.latency_ms.toFixed(1)} ms` : ''}</p>}</div>)}</div></td><td className="py-3 align-top text-xs">{Object.entries(item.checks).filter(([check])=>check!=='snmp').map(([check,result])=><div key={check}>{result.availability_pct != null ? `${result.availability_pct.toFixed(1)}% · ${result.samples} checks` : 'No samples'}</div>)}{kind==='devices' && item.checks.snmp?.state!=='disabled' && <div className="mt-2 text-muted">{item.checks.snmp?.samples || 0} SNMP samples</div>}</td></tr>)}
      </tbody></table></div>{filtered.length>limit && <Button variant="outline" size="sm" className="mt-3" onClick={()=>setLimit(limit+50)}>Show more ({Math.min(limit,filtered.length)} of {filtered.length})</Button>}
    </>}
  </CardContent></Card>
}

export function SensorDetailPage() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient(), can = useCan(), canManage = can('settings.manage')
  const [search,setSearch] = useState(''), [managing,setManaging] = useState(false)
  const [tokenInfo,setTokenInfo] = useState<TokenInfo | null>(null)
  const overview = useQuery<Overview>({ queryKey:['sensor-overview',id], queryFn:async()=>(await api.get(`/sensors/${id}/overview`)).data, enabled:!!id, refetchInterval:10000, retry:1 })
  const commands = useQuery<Command[]>({queryKey:['sensor-commands',id],queryFn:async()=>(await api.get(`/sensors/${id}/commands?limit=20`)).data,enabled:!!overview.data,refetchInterval:5000})
  const events = useQuery<Event[]>({queryKey:['sensor-events',id],queryFn:async()=>(await api.get(`/sensors/${id}/events?limit=10`)).data,enabled:!!overview.data,refetchInterval:15000})
  const refresh = () => { void qc.invalidateQueries({queryKey:['sensor-overview',id]});void qc.invalidateQueries({queryKey:['sensor-commands',id]});void qc.invalidateQueries({queryKey:['sensors']}) }
  const command = useMutation({mutationFn:async(verb:string)=>api.post(`/sensors/${id}/commands`,{verb,payload:{}}),onSuccess:()=>{toast.success('Command queued','The sensor will report the outcome after its next heartbeat.');refresh()},onError:e=>toast.error('Could not queue command',apiErrorMessage(e))})
  if (overview.isLoading) return <div className="p-8 text-sm text-muted">Loading sensor details…</div>
  if (!overview.data) return <div className="space-y-4 p-6"><Link className="text-primary" to="/settings/general?tab=sensors">← Back to sensors</Link><h1 className="text-xl font-semibold">Could not load sensor</h1><p className="text-sm text-muted">{apiErrorMessage(overview.error)}</p><Button onClick={()=>overview.refetch()}>Retry</Button></div>
  const {sensor,devices,services,release} = overview.data
  const signed = release.available, available = signed && newer(release.version || undefined,sensor.version)
  const activeCommand = (verb:string) => commands.data?.some(c=>c.verb===verb && ['pending','delivered'].includes(c.status) && Date.parse(c.expires_at)>Date.now())
  const allowed = canManage && !!sensor.api_key_prefix && sensor.status!=='disabled' && !sensor.authorization_pending
  return <div className="space-y-5">
    <Link className="inline-flex items-center gap-1 text-sm text-muted hover:text-primary" to="/settings/general?tab=sensors"><ArrowLeft className="h-4 w-4" />Back to sensors</Link>
    <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex flex-wrap items-center gap-3"><Radio className="h-6 w-6 text-primary" /><h1 className="text-2xl font-semibold">{sensor.name}</h1><Status state={sensor.status} /></div><p className="mt-1 text-sm text-muted">{sensor.site_name || 'Unassigned site'}{sensor.location ? ` · ${sensor.location}` : ''}{sensor.description ? ` — ${sensor.description}` : ''}</p></div><div className="flex gap-2"><Button variant="outline" size="sm" onClick={refresh} disabled={overview.isFetching}><RefreshCw className="h-4 w-4" />Refresh</Button>{canManage && <Button size="sm" onClick={()=>setManaging(true)}><Settings2 className="h-4 w-4" />Manage sensor</Button>}</div></div>
    {overview.isError && <p role="alert" className="rounded border border-warning p-3 text-sm text-warning">Refresh failed. Displaying the previous snapshot; its results may be stale.</p>}
    {!overview.data.measurements_available && <p role="alert" className="rounded border border-warning p-3 text-sm text-warning">Monitoring history is temporarily unavailable. Assignments remain visible; results are not being marked healthy.</p>}
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
      <Card><CardContent className="pt-5"><div className="flex items-center gap-2 text-xs text-muted"><Radio className="h-4 w-4" />Controller connection</div><div className="mt-2"><Status state={sensor.status} /></div><p className="mt-2 text-xs text-muted">Heartbeat {sensor.last_heartbeat_at ? relativeTime(sensor.last_heartbeat_at) : 'not received'}</p></CardContent></Card>
      <Card><CardContent className="pt-5"><div className="flex items-center gap-2 text-xs text-muted"><Server className="h-4 w-4" />Monitored devices</div><p className="mt-2 text-2xl font-semibold">{devices.length}</p><p className="mt-1 text-xs text-muted">{devices.filter(d=>d.checks.ping?.state!=='disabled').length} Ping · {devices.filter(d=>d.checks.snmp?.state!=='disabled').length} SNMP enabled</p></CardContent></Card>
      <Card><CardContent className="pt-5"><div className="flex items-center gap-2 text-xs text-muted"><Activity className="h-4 w-4" />Service checks</div><p className="mt-2 text-2xl font-semibold">{services.length}</p><p className="mt-1 text-xs text-muted">{services.filter(s=>s.checks.service.state==='up').length} up · {services.filter(s=>s.checks.service.state==='down').length} down from this sensor</p></CardContent></Card>
      <Card><CardContent className="pt-5"><div className="text-xs text-muted">Result delivery</div><p className="mt-2 text-2xl font-semibold">{sensor.queue_depth} <span className="text-sm font-normal">queued</span></p><p className="mt-1 text-xs text-muted">{sensor.queue_dropped_count} dropped results reported</p></CardContent></Card>
    </div>
    <div className="grid items-start gap-4 xl:grid-cols-2">
      <Card><CardHeader><CardTitle>Controller & appliance</CardTitle></CardHeader><CardContent><dl className="grid grid-cols-2 gap-4"><Detail label="Configured controller URL">{overview.data.controller_url}</Detail><Detail label="Last contact with controller">{relativeTime(sensor.last_seen_at)}</Detail><Detail label="Hostname">{sensor.hostname}</Detail><Detail label="Source IP seen by controller">{sensor.last_ip}</Detail><Detail label="Operating system">{sensor.os_info}</Detail><Detail label="Uptime at last heartbeat">{uptime(sensor.uptime_seconds)}</Detail><Detail label="Heartbeat interval">{sensor.heartbeat_interval_s}s</Detail><Detail label="Connection thresholds">Degraded after {sensor.degraded_after_s}s · offline after {sensor.offline_after_s}s</Detail></dl>{sensor.status_reason && <p className="mt-4 text-xs text-muted">{sensor.status_reason}</p>}<p className="mt-4 text-xs text-muted">The source IP may be a NAT or proxy address. Live status is based on the sensor's last heartbeat.</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Updates & configuration</CardTitle></CardHeader><CardContent className="space-y-4"><dl className="grid grid-cols-2 gap-4"><Detail label="Installed version">{sensor.version}</Detail><Detail label="Published signed version">{release.version || 'No signed release published'}</Detail></dl><p className="text-sm">{activeCommand('update') ? 'Update in progress — waiting for the sensor outcome.' : available ? 'A signed sensor update is available.' : signed && sensor.version ? 'No newer signed release is available.' : 'No signed update is available.'}</p><div className="flex flex-wrap gap-2">{canManage && <><Button size="sm" disabled={!allowed || !available || !!activeCommand('update') || command.isPending} onClick={()=>command.mutate('update')}><Download className="h-4 w-4" />Install update</Button><Button size="sm" variant="outline" disabled={!allowed || !!activeCommand('reload_config') || command.isPending} onClick={()=>command.mutate('reload_config')}>Reload monitoring configuration</Button></>}</div><p className="text-xs text-muted">Commands are delivered on the next heartbeat. Offline sensors receive queued commands when they reconnect, before the command expires.</p><h3 className="text-xs font-semibold">Recent commands</h3>{commands.isError ? <p className="text-xs text-danger">Command history could not be loaded.</p> : !commands.data?.length ? <p className="text-xs text-muted">No commands yet.</p> : <ul className="max-h-48 divide-y divide-border overflow-y-auto">{commands.data.map(c=><li key={c.id} className="flex items-start justify-between gap-3 py-2 text-xs"><div className="min-w-0"><p className="font-medium capitalize">{c.verb.replace(/_/g,' ')}{c.payload.version ? ` · ${c.payload.version}` : ''}</p>{c.result && <p className="mt-1 break-words text-muted">{c.result}</p>}<p className="mt-1 text-muted">{relativeTime(c.completed_at || c.created_at)}</p></div><Status command state={['pending','delivered'].includes(c.status)&&Date.parse(c.expires_at)<Date.now() ? 'expired' : c.status} /></li>)}</ul>}</CardContent></Card>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-lg font-semibold">Monitoring from this sensor</h2><p className="text-xs text-muted">Direct assignments, default-sensor assignments and inherited device groups. Open a target to manage its monitoring sites.</p></div><Input className="max-w-sm" aria-label="Search monitored targets" placeholder="Search devices, IPs or services…" value={search} onChange={e=>setSearch(e.target.value)} /></div>
    <TargetTable key={`devices-${id}-${search}`} title="Devices" items={devices} kind="devices" search={search} />
    <TargetTable key={`services-${id}-${search}`} title="Services" items={services} kind="services" search={search} />
    <p className="text-xs text-muted">Availability uses observed checks in the last 24 hours; missing results are not counted as success. SNMP freshness reflects the last received sample. Refreshes every 10 seconds · snapshot {new Date(overview.data.observed_at).toLocaleString()}.</p>
    <Card><CardHeader><CardTitle>Recent sensor activity</CardTitle></CardHeader><CardContent>{events.isError ? <p className="text-sm text-danger">Activity could not be loaded.</p> : !events.data?.length ? <p className="text-sm text-muted">No sensor events yet.</p> : <ul className="divide-y divide-border">{events.data.map(e=><li key={e.id} className="flex justify-between gap-4 py-2 text-xs"><div><span className="font-medium capitalize">{e.kind.replace(/_/g,' ')}</span>{e.detail.reason && <p className="mt-1 text-muted">{e.detail.reason}</p>}</div><time className="shrink-0 text-muted" title={new Date(e.ts).toLocaleString()}>{relativeTime(e.ts)}</time></li>)}</ul>}</CardContent></Card>
    <SensorDetailDialog sensor={managing ? sensor : null} onClose={()=>setManaging(false)} onChanged={refresh} onShowToken={t=>{setManaging(false);setTokenInfo(t)}} />
    <TokenDialog info={tokenInfo} onClose={()=>setTokenInfo(null)} />
  </div>
}
