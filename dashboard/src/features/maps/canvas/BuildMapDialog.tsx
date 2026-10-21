/* "Build from site…" (F2): ask the server to place a set of devices on the
 * map in a tiered layout (WAN edge → core → access by hop distance) and
 * create their fresh CDP/LLDP links. Three selectors: a location, an IPv4
 * subnet, or a seed device + hop depth. The server snapshots the map first. */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Loader2, MapPin, Network, Search, Sprout, Wand2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'

export type PopulateResult = {
  placed: number
  links: number
  skipped_existing: number
  version_id?: string | null
  tiers?: Record<string, number>
}

export type BuildMapDialogProps = {
  mapId: string
  onClose: () => void
  /** Called when the user dismisses the result view after a successful build
   *  (instead of onClose) — close the dialog there; the map query is already
   *  invalidated. */
  onBuilt: (r: PopulateResult) => void
  devices: { id: string; hostname: string; ip_address: string }[]
}

type Mode = 'location' | 'cidr' | 'seed'

const inputCls = 'w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60'

/** Strict IPv4 CIDR ("10.1.0.0/16"). Returns an error message or null. */
export function cidrError(value: string): string | null {
  const v = value.trim()
  if (!v) return 'Enter a subnet in CIDR form, e.g. 10.20.0.0/24'
  const m = v.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/)
  if (!m) return 'Use CIDR form a.b.c.d/nn, e.g. 10.20.0.0/24'
  const octets = m.slice(1, 5).map(Number)
  if (octets.some((o) => o > 255)) return 'Each address part must be 0–255'
  const prefix = Number(m[5])
  if (prefix > 32) return 'Prefix length must be 0–32'
  if (prefix < 8) return 'Prefix is too wide — use /8 or longer'
  return null
}

const MODES: { key: Mode; label: string; icon: React.ReactNode }[] = [
  { key: 'location', label: 'Site / location', icon: <MapPin className="h-3.5 w-3.5" /> },
  { key: 'cidr', label: 'Subnet', icon: <Network className="h-3.5 w-3.5" /> },
  { key: 'seed', label: 'Seed device', icon: <Sprout className="h-3.5 w-3.5" /> },
]

const TIER_LABEL: Record<string, string> = {
  '0': 'WAN edge', '1': 'Core', '2': 'Distribution', '3': 'Access',
  wan_edge: 'WAN edge', edge: 'WAN edge', core: 'Core', unconnected: 'Unconnected',
}

const tierLabel = (t: string) => TIER_LABEL[t] || (/^\d+$/.test(t) ? `Tier ${t}` : t.replace(/_/g, ' '))

export function BuildMapDialog({ mapId, onClose, onBuilt, devices }: BuildMapDialogProps) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<Mode>('location')
  const [location, setLocation] = useState('')
  const [cidr, setCidr] = useState('')
  const [cidrTouched, setCidrTouched] = useState(false)
  const [seedId, setSeedId] = useState('')
  const [seedQuery, setSeedQuery] = useState('')
  const [depth, setDepth] = useState(2)
  const [includeLinks, setIncludeLinks] = useState(true)
  const [replace, setReplace] = useState(false)
  const [result, setResult] = useState<PopulateResult | null>(null)

  const options = useQuery<{ locations: { name: string; device_count: number }[] }>({
    queryKey: ['manual-map-populate-options'],
    queryFn: async () => (await api.get('/maps/populate/options')).data,
  })
  const locations = options.data?.locations || []

  const sortedDevices = useMemo(
    () => devices.slice().sort((a, b) => (a.hostname || '').localeCompare(b.hostname || '')),
    [devices],
  )
  const filteredDevices = useMemo(() => {
    const q = seedQuery.trim().toLowerCase()
    const list = q
      ? sortedDevices.filter((d) => (d.hostname || '').toLowerCase().includes(q) || (d.ip_address || '').includes(q))
      : sortedDevices
    return list.slice(0, 200)
  }, [sortedDevices, seedQuery])
  const seed = devices.find((d) => d.id === seedId)

  const cidrErr = cidrError(cidr)
  const selectorOk = mode === 'location' ? !!location : mode === 'cidr' ? !cidrErr : !!seedId

  const build = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = { include_links: includeLinks, replace }
      if (mode === 'location') body.location = location
      else if (mode === 'cidr') body.cidr = cidr.trim()
      else { body.seed_device_id = seedId; body.depth = depth }
      return (await api.post(`/maps/${mapId}/populate`, body)).data as PopulateResult
    },
    onSuccess: (r) => {
      setResult(r)
      qc.invalidateQueries({ queryKey: ['manual-map', mapId] })
      qc.invalidateQueries({ queryKey: ['manual-map-versions', mapId] })
      qc.invalidateQueries({ queryKey: ['manual-map-suggested', mapId] })
    },
  })

  const dismiss = () => {
    if (build.isPending) return
    if (result) onBuilt(result)
    else onClose()
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) dismiss() }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Wand2 className="h-4 w-4 text-primary" /> Build map</DialogTitle>
          <DialogDescription className="text-xs">
            Places monitored devices in tiers (WAN edge, core, then by hop distance) and draws their current CDP/LLDP cables.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-start gap-2 rounded-lg border border-success/30 bg-success/10 p-3 text-xs text-text">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              <div>
                <div className="font-semibold">Map built</div>
                <div className="text-text2">
                  {result.placed} device{result.placed === 1 ? '' : 's'} placed · {result.links} link{result.links === 1 ? '' : 's'} created
                  {result.skipped_existing > 0 && <> · {result.skipped_existing} already on the map</>}
                </div>
              </div>
            </div>
            {result.tiers && Object.keys(result.tiers).length > 0 && (
              <div className="grid grid-cols-3 gap-2">
                {Object.entries(result.tiers).map(([tier, n]) => (
                  <div key={tier} className="rounded-md border border-border bg-surface2/40 px-2 py-1.5 text-center">
                    <div className="font-mono text-sm font-bold text-text">{n}</div>
                    <div className="text-[10px] text-muted">{tierLabel(tier)}</div>
                  </div>
                ))}
              </div>
            )}
            {result.version_id && (
              <p className="text-[11px] text-muted">The previous layout was saved as a version — use Versions to go back.</p>
            )}
            <DialogFooter>
              <Button size="sm" onClick={dismiss}>Done</Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {/* Mode tabs */}
            <div className="flex gap-1 rounded-lg border border-border bg-surface2/40 p-1">
              {MODES.map((m) => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => setMode(m.key)}
                  className={cn(
                    'flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-medium transition',
                    mode === m.key ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
                  )}
                >
                  {m.icon} {m.label}
                </button>
              ))}
            </div>

            {mode === 'location' && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium text-muted">Location</span>
                {options.isLoading ? (
                  <div className="flex items-center gap-2 py-1.5 text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading locations…</div>
                ) : options.isError ? (
                  <div className="text-danger">{apiErrorMessage(options.error, 'Could not load locations')}</div>
                ) : locations.length === 0 ? (
                  <div className="text-muted">No device has a location set. Use a subnet or a seed device instead.</div>
                ) : (
                  <select className={inputCls} value={location} onChange={(e) => setLocation(e.target.value)}>
                    <option value="">Choose a location…</option>
                    {locations.map((l) => (
                      <option key={l.name} value={l.name}>{l.name} ({l.device_count})</option>
                    ))}
                  </select>
                )}
              </label>
            )}

            {mode === 'cidr' && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium text-muted">Subnet (IPv4 CIDR)</span>
                <input
                  className={cn(inputCls, cidrTouched && cidrErr && 'border-danger/60')}
                  value={cidr}
                  placeholder="10.20.0.0/24"
                  onChange={(e) => setCidr(e.target.value)}
                  onBlur={() => setCidrTouched(true)}
                  autoFocus
                />
                {cidrTouched && cidrErr
                  ? <span className="text-[11px] text-danger">{cidrErr}</span>
                  : <span className="text-[11px] text-muted">Devices whose management IP is in this subnet.</span>}
              </label>
            )}

            {mode === 'seed' && (
              <div className="flex flex-col gap-2 text-xs">
                <span className="font-medium text-muted">Seed device</span>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                  <input
                    className={cn(inputCls, 'pl-7')}
                    value={seedQuery}
                    placeholder="Search hostname or IP…"
                    onChange={(e) => setSeedQuery(e.target.value)}
                    autoFocus
                  />
                </div>
                <div className="max-h-40 overflow-y-auto rounded-md border border-border">
                  {filteredDevices.length === 0 ? (
                    <div className="px-2 py-3 text-center text-muted">No matching device</div>
                  ) : filteredDevices.map((d) => (
                    <button
                      key={d.id}
                      type="button"
                      onClick={() => setSeedId(d.id)}
                      className={cn(
                        'flex w-full items-center justify-between gap-2 px-2 py-1 text-left transition',
                        d.id === seedId ? 'bg-primary/15 text-text' : 'text-text2 hover:bg-primary/10',
                      )}
                    >
                      <span className="truncate font-medium">{d.hostname}</span>
                      <span className="shrink-0 font-mono text-[10px] text-muted">{d.ip_address}</span>
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-muted">Selected: <b className="text-text">{seed ? seed.hostname : '—'}</b></span>
                  <label className="ml-auto flex items-center gap-2">
                    <span className="font-medium text-muted">Hops</span>
                    <select className={cn(inputCls, 'w-16')} value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
                      {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                  </label>
                </div>
              </div>
            )}

            <div className="flex flex-col gap-1.5 border-t border-border/60 pt-3 text-xs">
              <label className="flex items-center gap-2 text-text">
                <input type="checkbox" checked={includeLinks} onChange={(e) => setIncludeLinks(e.target.checked)} />
                Include discovered links (fresh CDP/LLDP only)
              </label>
              <label className="flex items-center gap-2 text-text">
                <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
                Replace existing nodes
              </label>
              {replace && (
                <div className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
                  <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
                  Nodes already on this map (and their cables) are removed before building. A snapshot is taken first,
                  so you can restore it from Versions.
                </div>
              )}
            </div>

            {build.isError && (
              <div className="rounded-md border border-danger/40 bg-danger/10 px-2 py-1.5 text-[11px] text-danger">
                {apiErrorMessage(build.error, 'Build failed')}
              </div>
            )}

            <DialogFooter>
              <Button size="sm" variant="outline" onClick={onClose} disabled={build.isPending}>Cancel</Button>
              <Button
                size="sm"
                onClick={() => { setCidrTouched(true); if (selectorOk) build.mutate() }}
                disabled={build.isPending || (mode !== 'cidr' && !selectorOk)}
              >
                {build.isPending ? <Loader2 className="animate-spin" /> : <Wand2 />} Build
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
