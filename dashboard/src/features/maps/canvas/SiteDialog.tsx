import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Building2 } from 'lucide-react'
import { api } from '@/lib/api'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import type { ManualMapListItem, SiteSpec } from '../core'

export type SiteDialogResult = {
  name: string
  spec: SiteSpec
  /** Create a new drill-down map for the site (named after it). */
  createMap: boolean
  /** Fill the new map from the location's devices and their discovered links. */
  buildFromLocation: boolean
  /** Move the picked devices off this map into the new site map. */
  moveSelection: boolean
}

const inputCls = 'w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60'

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="text-2xs text-muted">{hint}</span>}
    </label>
  )
}

/** Create or edit a site node. A site groups devices by location, by its own
 *  drill-down map and/or by hand; the dialog can also create that map. */
export function SiteDialog({ mode, mapId, maps, initialName, initialSpec, pickedCount, onCancel, onSave, saving }: {
  mode: 'create' | 'edit'
  mapId: string
  maps: ManualMapListItem[]
  initialName?: string
  initialSpec?: SiteSpec | null
  /** Devices selected on the canvas when "Group into site" was chosen. */
  pickedCount?: number
  onCancel: () => void
  onSave: (r: SiteDialogResult) => void
  saving?: boolean
}) {
  const spec0 = initialSpec || {}
  const [name, setName] = useState(initialName || '')
  const [location, setLocation] = useState(spec0.location || '')
  const [mapChoice, setMapChoice] = useState<string>(spec0.map_id || (pickedCount ? '__new' : ''))
  const [build, setBuild] = useState(true)
  const [move, setMove] = useState(true)
  const [expanded, setExpanded] = useState(!!spec0.expanded)
  const [keepPicked, setKeepPicked] = useState(true)
  const locations = useQuery<{ locations: { name: string; device_count: number }[] }>({
    queryKey: ['manual-map-populate-options'],
    queryFn: async () => (await api.get('/maps/populate/options')).data,
    staleTime: 60_000,
  })
  const otherMaps = useMemo(() => maps.filter((m) => m.id !== mapId).sort((a, b) => a.name.localeCompare(b.name)), [maps, mapId])
  const picked = spec0.device_ids?.length || 0
  const creatingMap = mapChoice === '__new'
  const effectiveName = name.trim() || location.trim() || ''
  const hasMembers = !!location.trim() || (!!mapChoice && mapChoice !== '') || (keepPicked && (picked > 0 || (pickedCount || 0) > 0))

  const submit = () => {
    if (!effectiveName) return
    onSave({
      name: effectiveName,
      spec: {
        location: location.trim() || null,
        map_id: creatingMap ? null : (mapChoice || null),
        device_ids: keepPicked ? (spec0.device_ids || null) : null,
        expanded,
      },
      createMap: creatingMap,
      buildFromLocation: creatingMap && build && !!location.trim() && !(pickedCount && move),
      moveSelection: creatingMap && !!pickedCount && move,
    })
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Building2 className="h-4 w-4 text-primary" />{mode === 'create' ? 'New site' : 'Edit site'}</DialogTitle>
        </DialogHeader>
        <DialogDescription>
          A site is one tile that stands for many devices: it shows their combined status and alerts, lists every
          device on demand and opens the site&apos;s own map.
        </DialogDescription>

        <Field label="Site name">
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder={location || 'e.g. Riyadh branch'} autoFocus />
        </Field>

        <Field label="Devices at this location" hint="Every device whose Location matches is counted, including devices added later.">
          <input className={inputCls} list="zp-site-locations" value={location} onChange={(e) => setLocation(e.target.value)} placeholder="No location" />
          <datalist id="zp-site-locations">
            {(locations.data?.locations || []).map((l) => <option key={l.name} value={l.name}>{`${l.device_count} devices`}</option>)}
          </datalist>
        </Field>

        <Field label="Drill-down map" hint="Its nodes count as members too, and the tile's Open button leads there.">
          <select className={inputCls} value={mapChoice} onChange={(e) => setMapChoice(e.target.value)}>
            <option value="">None</option>
            <option value="__new">Create a new map for this site…</option>
            {otherMaps.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.node_count} nodes</option>)}
          </select>
        </Field>

        {creatingMap && !!pickedCount && (
          <label className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 p-2 text-xs text-text2">
            <input type="checkbox" checked={move} onChange={(e) => setMove(e.target.checked)} className="mt-0.5 accent-primary" />
            <span>
              Move the {pickedCount} selected item{pickedCount > 1 ? 's' : ''} into the new site map (cables between them go along;
              cables to the rest of this map are replaced by the site tile). Ctrl+Z puts them back.
            </span>
          </label>
        )}
        {creatingMap && !(pickedCount && move) && (
          <label className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 p-2 text-xs text-text2">
            <input type="checkbox" checked={build} disabled={!location.trim()} onChange={(e) => setBuild(e.target.checked)} className="mt-0.5 accent-primary" />
            <span>Build the new map from the location&apos;s devices and their discovered links{location.trim() ? '' : ' (choose a location first)'}.</span>
          </label>
        )}

        {picked > 0 && (
          <label className="flex items-center gap-2 text-xs text-text2">
            <input type="checkbox" checked={keepPicked} onChange={(e) => setKeepPicked(e.target.checked)} className="accent-primary" />
            Keep the {picked} hand-picked device{picked > 1 ? 's' : ''}
          </label>
        )}

        <label className="flex items-center gap-2 text-xs text-text2">
          <input type="checkbox" checked={expanded} onChange={(e) => setExpanded(e.target.checked)} className="accent-primary" />
          Open the device grid by default in Live and on the wall
        </label>

        {!hasMembers && !creatingMap && <div className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-xs text-warning">Choose a location or a drill-down map so the site has members.</div>}

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button onClick={submit} disabled={saving || !effectiveName}>{saving ? 'Working…' : mode === 'create' ? 'Create site' : 'Save'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
