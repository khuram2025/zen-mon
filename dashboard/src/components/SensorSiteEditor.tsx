import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'

export function SensorSiteEditor({ sensor, onChanged }: { sensor: { id: string; site_id: string | null; location: string | null }; onChanged: () => void }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [siteId, setSiteId] = useState(sensor.site_id || '')
  const [location, setLocation] = useState(sensor.location || '')
  const [name, setName] = useState('')
  const sites = useQuery<Array<{ id: string; name: string }>>({ queryKey: ['sites'], queryFn: async () => (await api.get('/sites')).data })
  const create = useMutation({
    mutationFn: async () => (await api.post('/sites', { name: name.trim() })).data,
    onSuccess: site => { setSiteId(site.id); setName(''); qc.invalidateQueries({ queryKey: ['sites'] }); toast.success('Site created') },
    onError: e => toast.error('Could not create site', apiErrorMessage(e)),
  })
  const save = useMutation({
    mutationFn: () => api.put(`/sensors/${sensor.id}`, { site_id: siteId || null, location: location.trim() || null }),
    onSuccess: () => { setEditing(false); onChanged(); qc.invalidateQueries({ queryKey: ['sites'] }); qc.invalidateQueries({ queryKey: ['monitoring-sites'] }); toast.success('Sensor location saved') },
    onError: e => toast.error('Could not save location', apiErrorMessage(e)),
  })
  if (!editing) return <Button variant="outline" size="sm" onClick={() => { setSiteId(sensor.site_id || ''); setLocation(sensor.location || ''); setEditing(true) }}>Edit site / location</Button>
  return <div className="space-y-3 rounded-lg border border-border p-3">
    <h4 className="text-sm font-semibold">Sensor site / location</h4>
    <label className="block space-y-1 text-xs">Site
      <select className="block w-full rounded border border-border bg-surface p-2 text-text" value={siteId} onChange={e => setSiteId(e.target.value)}>
        <option value="">Unassigned site</option>{sites.data?.map(site => <option key={site.id} value={site.id}>{site.name}</option>)}
      </select>
    </label>
    <label className="block space-y-1 text-xs">Location<Input value={location} onChange={e => setLocation(e.target.value)} placeholder="Building, branch or cloud region" /></label>
    <div className="flex items-end gap-2"><label className="flex-1 space-y-1 text-xs">New site name<Input value={name} onChange={e => setName(e.target.value)} placeholder="Create a site" /></label><Button variant="outline" size="sm" disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>Create site</Button></div>
    <div className="flex justify-end gap-2"><Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button><Button size="sm" disabled={save.isPending || create.isPending} onClick={() => save.mutate()}>Save site / location</Button></div>
  </div>
}
