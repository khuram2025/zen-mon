import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { MapPin, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'

type Observation = { state: string; last_result_at: string | null; latency_ms: number | null; availability_pct: number | null; samples: number }
type Sensor = { id: string; name: string; site_name: string | null; location: string | null; status: string; available: boolean }
type Site = { poller_id: string; name: string; location: string | null; sensor_name: string | null; probe_status: string; checks: Record<string, Observation> }
type Monitoring = { controller_enabled: boolean; sensor_ids: string[]; sites: Site[]; available_sensors: Sensor[]; remote_supported: boolean }

const labels: Record<string, string> = { up: 'Up', down: 'Down', no_data: 'No recent data', disabled: 'Disabled', probe_offline: 'Probe offline', probe_disabled: 'Probe disabled', probe_pending: 'Awaiting authorization' }

export function MonitoringSites({ targetType, targetId, compact = false }: { targetType: 'device' | 'service_check'; targetId: string; compact?: boolean }) {
  const qc = useQueryClient()
  const key = ['monitoring-sites', targetType, targetId]
  const endpoint = `/monitoring-sites/${targetType}/${targetId}`
  const query = useQuery<Monitoring>({ queryKey: key, queryFn: async () => (await api.get(endpoint)).data, refetchInterval: 15_000 })
  const [editing, setEditing] = useState(false)
  const [controller, setController] = useState(true)
  const [picked, setPicked] = useState<string[]>([])
  const save = useMutation({
    mutationFn: async () => api.put(endpoint, { controller_enabled: controller, sensor_ids: picked }),
    onSuccess: () => {
      setEditing(false)
      qc.invalidateQueries({ queryKey: ['monitoring-sites'] })
      qc.invalidateQueries({ queryKey: ['sensors'] })
      qc.invalidateQueries({ queryKey: ['sensor-assignments'] })
      toast.success('Monitoring sites saved', 'Results will arrive after the next configuration refresh and check interval.')
    },
    onError: (e) => toast.error('Could not save monitoring sites', apiErrorMessage(e)),
  })
  const openEditor = () => {
    setController(query.data?.controller_enabled ?? true)
    setPicked(query.data?.sensor_ids ?? [])
    setEditing(true)
  }
  const groups = new Map<string, Sensor[]>()
  for (const sensor of query.data?.available_sensors ?? []) {
    const name = sensor.site_name || sensor.location || 'Unassigned site'
    groups.set(name, [...(groups.get(name) || []), sensor])
  }
  return (
    <section className={compact ? 'mt-4 border-t border-border/60 pt-3' : 'rounded-lg border border-border bg-surface p-4'}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold"><MapPin className="h-4 w-4 text-primary" />Site Availability</h3>
        <Button size="sm" variant="ghost" onClick={openEditor} disabled={!query.data}><Settings2 className="h-3.5 w-3.5" />Manage sites</Button>
      </div>
      {query.isLoading && <p className="text-xs text-muted">Loading monitoring locations…</p>}
      {query.isError && <div className="text-xs text-danger">Could not load site availability. <button className="underline" onClick={() => query.refetch()}>Retry</button></div>}
      <div className={compact ? 'space-y-2' : 'grid gap-3 md:grid-cols-2 xl:grid-cols-3'}>
        {query.data?.sites.map(site => (
          <div key={site.poller_id} className="min-w-0 rounded-md border border-border/60 bg-surface2/30 p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0"><div className="truncate text-xs font-semibold">{site.name}</div>{site.sensor_name && <div className="mt-0.5 truncate text-[11px] text-muted">{site.sensor_name}{site.location && site.location !== site.name ? ` · ${site.location}` : ''}</div>}</div>
              {site.poller_id === 'central' && <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">Default</span>}
            </div>
            {Object.entries(site.checks).map(([type, result]) => (
              <div key={type} className="mt-2 text-[11px]">
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <span className="font-medium">{type === 'service' ? 'Service' : type.toUpperCase()}</span>
                  <span className={result.state === 'up' ? 'text-success' : result.state === 'down' ? 'text-danger' : 'text-muted'}>{labels[result.state] || result.state}</span>
                </div>
                {result.state !== 'disabled' && <div className="mt-0.5 flex flex-wrap justify-between gap-1 text-muted">
                  <span>{result.last_result_at ? relativeTime(result.last_result_at) : 'Awaiting first result'}{result.latency_ms != null ? ` · ${result.latency_ms.toFixed(1)} ms` : ''}</span>
                  {result.availability_pct != null && <span title={`${result.samples} observed checks in the last 24 hours`}>{result.availability_pct.toFixed(1)}% / 24h</span>}
                </div>}
              </div>
            ))}
          </div>
        ))}
      </div>
      {query.data && <p className="mt-2 text-[10px] leading-relaxed text-muted">Availability is based on observed checks over 24 hours. Missing results are not counted as success. {query.data.controller_enabled ? 'Overall status uses the controller.' : targetType === 'device' ? 'Overall status uses the primary selected probe.' : 'Overall status uses the configured sensor consensus.'}</p>}
      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="max-h-[85vh] max-w-lg overflow-y-auto">
          <DialogHeader><DialogTitle>Monitoring sites</DialogTitle></DialogHeader>
          <p className="text-xs text-muted">Choose one or more locations. Each selected sensor runs the enabled {targetType === 'device' ? 'Ping and SNMP checks' : 'service check'} from its network.</p>
          <label className="flex cursor-pointer items-center gap-3 rounded-md border border-border p-3">
            <input type="checkbox" checked={controller} onChange={e => setController(e.target.checked)} />
            <span className="text-sm font-medium">Controller <span className="text-xs font-normal text-muted">(default)</span></span>
          </label>
          {[...groups.entries()].map(([name, sensors]) => <div key={name} className="space-y-1">
            <h4 className="flex items-center gap-1 py-1 text-xs font-semibold"><MapPin className="h-3 w-3" />{name}</h4>
            {sensors.map(sensor => {
              const selected = picked.includes(sensor.id)
              return <label key={sensor.id} className="flex items-center justify-between gap-3 rounded border border-border/60 p-2 text-xs">
                <span className="flex items-center gap-2"><input type="checkbox" checked={selected} disabled={(!sensor.available || !query.data?.remote_supported) && !selected}
                  onChange={e => setPicked(e.target.checked ? [...picked, sensor.id] : picked.filter(id => id !== sensor.id))} />{sensor.name}{sensor.location && sensor.location !== name ? ` · ${sensor.location}` : ''}</span>
                <span className={sensor.available ? 'text-success' : 'text-muted'}>{sensor.status === 'update_required' ? 'Update required (1.23.5+)' : sensor.status}</span>
              </label>
            })}
          </div>)}
          {groups.size === 0 && <p className="text-xs text-muted">No sensors are registered yet.</p>}
          <Link to="/settings/general?tab=sensors" className="text-xs text-primary hover:underline">Assign sensor sites and locations in Settings → Sensors</Link>
          <DialogFooter><Button variant="outline" onClick={() => setEditing(false)}>Cancel</Button><Button disabled={save.isPending || (!controller && !picked.length)} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save monitoring sites'}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
