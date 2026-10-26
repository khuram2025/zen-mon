import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Cable, ChevronRight, History, Link2Off, Loader2, Search, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { nf, nfKeys, nfRetry, useNfQuery } from '@/lib/netflow/api'
import { apiErrorMessage } from '@/lib/netflow/status'
import { formatCount, formatDateTime, ipDisplay } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import {
  impactLines,
  reverseDnsSourceText,
  type ExporterDeviceRow,
  type HistoryEntry,
  type MappingImpact,
  type SuggestionCandidate,
  type Suggestions,
} from '../health/model'
import { ToneChip, useDebounced } from '../health/ui'

/* ─────────────────────────────────────────────────────────────────
   Exporter → device mapping (FE-08)

   Flow records identify interfaces only by (exporter IP, ifIndex). Names, speeds and team scope
   come from the device that owns the exporter: an explicit link here wins over a management-IP
   match everywhere. Choosing a device is server-side searchable (any fleet size), shows the
   evidence behind suggestions, previews what the link changes before it is confirmed, and keeps
   the change history.
   ───────────────────────────────────────────────────────────────── */

export interface ExporterDeviceDialogProps {
  open: boolean
  onOpenChange: (v: boolean) => void
  /** Legacy list-mode input (overview page). The dialog loads its own searchable list; these rows are shown until it arrives. */
  exporters?: ExporterDeviceRow[]
  /** Open directly on one exporter. */
  exporter?: string | null
}

const PAGE = 25

function ExporterDeviceDialog({ open, onOpenChange, exporters, exporter }: ExporterDeviceDialogProps) {
  const [selected, setSelected] = useState<string | null>(exporter ?? null)
  useEffect(() => {
    if (open) setSelected(exporter ?? null)
  }, [open, exporter])
  const fromList = !exporter

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[92vh] w-[calc(100vw-1rem)] max-w-3xl overflow-y-auto overflow-x-hidden p-4 focus:outline-none sm:p-6"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement | null)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-6">
            <Cable className="h-4 w-4 shrink-0 text-primary" aria-hidden />
            {selected ? `Link exporter ${ipDisplay(selected)}` : 'Flow exporters and their devices'}
          </DialogTitle>
          <DialogDescription>
            Flows name interfaces only by number. Linking an exporter to the device it belongs to gives its interfaces
            real names and speeds, and puts its flows in that device&apos;s team scope. A link overrides a
            management-address match.
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0">
          {selected ? (
            <ExporterMapping
              exporter={selected}
              onBack={fromList ? () => setSelected(null) : undefined}
              onDone={fromList ? () => setSelected(null) : () => onOpenChange(false)}
            />
          ) : (
            <ExporterList initial={exporters} onPick={setSelected} />
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ─── List mode ────────────────────────────────────────────────────────── */

function ExporterList({ initial, onPick }: { initial?: ExporterDeviceRow[]; onPick: (ip: string) => void }) {
  const [q, setQ] = useState('')
  const [unmapped, setUnmapped] = useState(false)
  const [page, setPage] = useState(0)
  const search = useDebounced(q.trim(), 250)
  useEffect(() => setPage(0), [search, unmapped])
  const params = { search: search || undefined, unmapped: unmapped || undefined, offset: page * PAGE, limit: PAGE, tz: displayTimeZone() }
  const list = useNfQuery<ExporterDeviceRow[]>('/netflow/exporter-devices', params, { refetchInterval: false, staleTime: 30_000 })
  const rows = list.data?.data ?? (list.status === 'pending' && initial && !search && !unmapped ? initial.slice(0, PAGE) : undefined)
  const more = list.data?.meta?.truncated?.more ?? false
  const inputId = useId()
  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor={inputId} className="sr-only">Search exporters</label>
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text2" aria-hidden />
          <input
            id={inputId}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Exporter address, name or device"
            className="h-9 w-full rounded-md border border-border bg-surface pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
          />
        </div>
        <label className="inline-flex items-center gap-2 text-xs text-text2">
          <input type="checkbox" checked={unmapped} onChange={(e) => setUnmapped(e.target.checked)} className="h-4 w-4" />
          Without a device only
        </label>
      </div>
      <QueryPanel
        query={{ ...list, data: rows, status: rows ? 'success' : list.status }}
        label="exporters"
        skeleton="rows"
        compact
        empty={search || unmapped ? 'No exporter matches' : 'No exporter has sent flows in the last 7 days'}
      >
        {(items) => (
          <ul className="divide-y divide-border rounded-md border border-border">
            {items.map((e) => (
              <li key={e.exporter_ip} className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
                <div className="min-w-0 flex-1">
                  <div className="font-mono font-medium text-text">
                    {ipDisplay(e.exporter_ip)}
                    {e.display_name && <span className="ml-2 font-sans text-text2">{e.display_name}</span>}
                  </div>
                  <div className="mt-0.5 text-xs text-text2">
                    {formatCount(e.flows)} records in 7 days ·{' '}
                    {e.source === 'mapped' && <>linked to <span className="font-medium text-text">{e.device_hostname}</span></>}
                    {e.source === 'ip-match' && <>management address of <span className="font-medium text-text">{e.device_hostname}</span></>}
                    {!e.source && 'no device — interfaces show as numbers'}
                    {e.overrides_device_hostname && <> (overrides {e.overrides_device_hostname})</>}
                  </div>
                </div>
                <Button size="sm" variant={e.source ? 'outline' : 'default'} className="h-8" onClick={() => onPick(e.exporter_ip)}>
                  {e.source ? 'Review link' : 'Link to a device'}
                  <ChevronRight aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </QueryPanel>
      {(page > 0 || more) && (
        <div className="flex items-center justify-between text-xs text-text2">
          <Button size="sm" variant="outline" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>Previous</Button>
          <span>Page {page + 1}</span>
          <Button size="sm" variant="outline" disabled={!more} onClick={() => setPage((p) => p + 1)}>Next</Button>
        </div>
      )}
    </div>
  )
}

/* ─── One exporter ─────────────────────────────────────────────────────── */

interface PickedDevice {
  device_id: string
  hostname: string
  ip_address: string | null
}

export function ExporterMapping({ exporter, onBack, onDone }: { exporter: string; onBack?: () => void; onDone?: () => void }) {
  const can = useCan()
  const canManage = can('netflow.manage')
  const qc = useQueryClient()
  const [picked, setPicked] = useState<PickedDevice | null>(null)
  const [note, setNote] = useState('')
  const [confirmUnlink, setConfirmUnlink] = useState(false)
  const enc = encodeURIComponent(exporter)

  const suggestions = useNfQuery<Suggestions>(`/netflow/exporter-devices/${enc}/suggestions`, null, { refetchInterval: false, staleTime: 60_000 })
  const history = useNfQuery<HistoryEntry[]>(`/netflow/exporter-devices/${enc}/history`, null, { refetchInterval: false })
  const owner = suggestions.data?.data.current_owner ?? null

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: nfKeys.all })
  }
  const save = useMutation({
    mutationFn: async (d: PickedDevice) => api.put(`/netflow/exporter-devices/${enc}`, { device_id: d.device_id, note: note.trim() || null }),
    onSuccess: (_r, d) => {
      toast.success('Exporter linked', `${ipDisplay(exporter)} now belongs to ${d.hostname}.`)
      invalidate()
      setPicked(null)
      setNote('')
      onDone?.()
    },
    onError: (e) => toast.error('Could not link the exporter', apiErrorMessage(e)),
  })
  const unlink = useMutation({
    mutationFn: async () => api.delete(`/netflow/exporter-devices/${enc}`),
    onSuccess: () => {
      toast.success('Link removed', `${ipDisplay(exporter)} falls back to a management-address match, if any.`)
      invalidate()
      setConfirmUnlink(false)
    },
    onError: (e) => toast.error('Could not remove the link', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      {onBack && (
        <button type="button" onClick={onBack} className="inline-flex items-center gap-1 text-xs text-text2 hover:text-text">
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> All exporters
        </button>
      )}
      <section aria-label="Current owner" className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs">
        <span className="font-mono font-semibold text-text">{ipDisplay(exporter)}</span>
        {suggestions.status === 'success' &&
          (owner ? (
            <>
              <span className="text-text2">{owner.source === 'mapped' ? 'is linked to' : 'is the management address of'}</span>
              <Link to={`/devices/${owner.device_id}`} className="font-medium text-text underline-offset-2 hover:underline">{owner.hostname}</Link>
              <ToneChip tone={owner.source === 'mapped' ? 'success' : 'info'}>{owner.source === 'mapped' ? 'Linked' : 'Address match'}</ToneChip>
              {owner.source === 'mapped' && canManage && (
                <Button size="sm" variant="outline" className="ml-auto h-7" onClick={() => setConfirmUnlink(true)} disabled={unlink.isPending}>
                  <Link2Off aria-hidden /> Remove link
                </Button>
              )}
            </>
          ) : (
            <span className="text-text2">is not linked to any device; its interfaces show as numbers.</span>
          ))}
      </section>
      {confirmUnlink && owner && (
        <div role="alertdialog" aria-label="Confirm removing the link" className="rounded-md border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-text">
          <p>
            Remove the link to <span className="font-semibold">{owner.hostname}</span>? Interface names, speeds and team visibility
            will fall back to a management-address match, or to none.
          </p>
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="destructive" onClick={() => unlink.mutate()} disabled={unlink.isPending}>
              {unlink.isPending && <Loader2 className="animate-spin" aria-hidden />} Remove link
            </Button>
            <Button size="sm" variant="outline" onClick={() => setConfirmUnlink(false)}>Keep it</Button>
          </div>
        </div>
      )}

      {!canManage && (
        <p className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-text2">
          You can review the evidence, but changing a link needs the NetFlow manage permission.
        </p>
      )}

      {picked ? (
        <ImpactReview
          exporter={exporter}
          device={picked}
          note={note}
          setNote={setNote}
          canManage={canManage}
          saving={save.isPending}
          onCancel={() => setPicked(null)}
          onConfirm={() => save.mutate(picked)}
        />
      ) : (
        <>
          <section aria-labelledby="nf-map-suggest">
            <h3 id="nf-map-suggest" className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-text">
              <Sparkles className="h-4 w-4 text-primary" aria-hidden /> Suggested devices
            </h3>
            <QueryPanel
              query={suggestions}
              label="suggestions"
              skeleton="rows"
              compact
              isEmpty={(r) => r.data.candidates.length === 0}
              empty="No device in your scope has evidence of owning this address — search for it below"
            >
              {(r) => (
                <>
                  <ul className="space-y-2">
                    {r.data.candidates.map((c) => (
                      <CandidateCard key={c.device_id} c={c} canManage={canManage} onChoose={() => setPicked(c)} />
                    ))}
                  </ul>
                  <SourcesNote s={r.data} />
                </>
              )}
            </QueryPanel>
          </section>
          {canManage && (
            <section aria-labelledby="nf-map-search">
              <h3 id="nf-map-search" className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-text">
                <Search className="h-4 w-4 text-primary" aria-hidden /> Find a device
              </h3>
              <DevicePicker onPick={setPicked} />
            </section>
          )}
        </>
      )}

      <details className="rounded-md border border-border px-3 py-2 text-xs [&_summary]:cursor-pointer">
        <summary className="flex items-center gap-1.5 font-semibold text-text">
          <History className="h-3.5 w-3.5" aria-hidden /> Change history
        </summary>
        <div className="mt-2">
          <QueryPanel query={history} label="the change history" skeleton="rows" compact empty="No link or settings change has been recorded">
            {(r) => <HistoryList rows={r.data} />}
          </QueryPanel>
        </div>
      </details>
    </div>
  )
}

function CandidateCard({ c, canManage, onChoose }: { c: SuggestionCandidate; canManage: boolean; onChoose: () => void }) {
  const tone = c.confidence === 'high' ? 'success' : c.confidence === 'medium' ? 'info' : 'unknown'
  return (
    <li className="rounded-md border border-border px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Link to={`/devices/${c.device_id}`} className="font-semibold text-text underline-offset-2 hover:underline">{c.hostname}</Link>
        <span className="font-mono text-text2">{c.ip_address ?? 'no address'}</span>
        {(c.vendor || c.model) && <span className="text-text2">{[c.vendor, c.model].filter(Boolean).join(' ')}</span>}
        <ToneChip tone={tone}>{c.confidence === 'high' ? 'Strong evidence' : c.confidence === 'medium' ? 'Some evidence' : 'Weak evidence'}</ToneChip>
        {c.current_owner && <ToneChip tone="neutral" icon={null}>Current owner</ToneChip>}
        {canManage && !c.current_owner && (
          <Button size="sm" className="ml-auto h-7" onClick={onChoose}>Choose</Button>
        )}
      </div>
      <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-text2">
        {c.evidence.map((e, i) => <li key={i}>{e.text}</li>)}
      </ul>
      {c.guidance.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {c.guidance.map((g) => (
            <li key={g.code}><ToneChip tone="warning" className="whitespace-normal">{g.message}</ToneChip></li>
          ))}
        </ul>
      )}
    </li>
  )
}

function SourcesNote({ s }: { s: Suggestions }) {
  const parts: string[] = ['management and reported addresses', 'User Device Tracker ARP evidence']
  if (s.sources.ncm.permitted) {
    parts.push(s.sources.ncm.available ? `running configurations (${s.sources.ncm.scanned} scanned${s.sources.ncm.complete ? '' : ', partial'})` : 'running configurations (unavailable)')
  }
  parts.push(reverseDnsSourceText(s))
  return (
    <p className="mt-2 text-xs text-text2">
      Evidence checked: {parts.join(', ')}.
      {!s.sources.ncm.permitted && ' Configuration evidence needs the NCM view permission.'}
    </p>
  )
}

/* ─── Server-side device search ────────────────────────────────────────── */

interface DeviceListRow {
  id: string
  hostname: string
  ip_address: string | null
  vendor?: string | null
  model?: string | null
  status?: string | null
  tags?: string[] | null
}

function DevicePicker({ onPick }: { onPick: (d: PickedDevice) => void }) {
  const [q, setQ] = useState('')
  const [skip, setSkip] = useState(0)
  const [active, setActive] = useState(0)
  const search = useDebounced(q.trim(), 250)
  useEffect(() => { setSkip(0); setActive(0) }, [search])
  const devices = useQuery<{ data: DeviceListRow[]; meta: { total: number; skip: number; limit: number } }>({
    queryKey: ['devices', 'netflow-picker', search, skip],
    queryFn: async () => (await api.get('/devices', { params: { search: search || undefined, limit: 20, skip } })).data,
    placeholderData: keepPreviousData,
    retry: nfRetry,
    staleTime: 30_000,
  })
  const rows = devices.data?.data ?? []
  const total = devices.data?.meta.total ?? 0
  const listId = useId()
  const inputId = useId()
  const listRef = useRef<HTMLUListElement>(null)
  const choose = (d: DeviceListRow) => onPick({ device_id: d.id, hostname: d.hostname, ip_address: d.ip_address })
  const onKey = (e: React.KeyboardEvent) => {
    if (!rows.length) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(rows.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); const d = rows[active]; if (d) choose(d) }
  }
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])
  return (
    <div className="space-y-2">
      <label htmlFor={inputId} className="sr-only">Search devices by name, address or tag</label>
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text2" aria-hidden />
        <input
          id={inputId}
          role="combobox"
          aria-expanded={rows.length > 0}
          aria-controls={listId}
          aria-activedescendant={rows[active] ? `${listId}-${active}` : undefined}
          aria-autocomplete="list"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder="Device name, management address or tag"
          className="h-9 w-full rounded-md border border-border bg-surface pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
        />
      </div>
      <QueryPanel
        query={devices}
        label="devices"
        skeleton="rows"
        compact
        isEmpty={(r) => r.data.length === 0}
        empty={search ? `No device in your scope matches “${search}”` : 'No devices in your scope'}
      >
        {() => (
          <ul id={listId} ref={listRef} role="listbox" aria-label="Devices" className="max-h-64 divide-y divide-border overflow-y-auto rounded-md border border-border">
            {rows.map((d, i) => (
              <li
                key={d.id}
                id={`${listId}-${i}`}
                data-idx={i}
                role="option"
                aria-selected={i === active}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(d)}
                className={cn('flex cursor-pointer flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-2 text-xs', i === active ? 'bg-primary/10' : 'hover:bg-surface2')}
              >
                <span className="font-medium text-text">{d.hostname}</span>
                <span className="font-mono text-text2">{d.ip_address ? String(d.ip_address).split('/')[0] : 'no address'}</span>
                {(d.vendor || d.model) && <span className="text-text2">{[d.vendor, d.model].filter(Boolean).join(' ')}</span>}
                {d.tags && d.tags.length > 0 && <span className="text-text2">tags: {d.tags.join(', ')}</span>}
              </li>
            ))}
          </ul>
        )}
      </QueryPanel>
      {total > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
          <span>Showing {skip + 1}–{skip + rows.length} of {total.toLocaleString()} devices · ↑ ↓ to move, Enter to choose</span>
          <span className="flex gap-2">
            <Button size="sm" variant="outline" className="h-7" disabled={skip === 0} onClick={() => setSkip((s) => Math.max(0, s - 20))}>Previous</Button>
            <Button size="sm" variant="outline" className="h-7" disabled={skip + rows.length >= total} onClick={() => setSkip((s) => s + 20)}>Next</Button>
          </span>
        </div>
      )}
    </div>
  )
}

/* ─── Confirmation with the effect of the link ─────────────────────────── */

function ImpactReview({ exporter, device, note, setNote, canManage, saving, onCancel, onConfirm }: {
  exporter: string
  device: PickedDevice
  note: string
  setNote: (v: string) => void
  canManage: boolean
  saving: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const enc = encodeURIComponent(exporter)
  const impact = useQuery({
    queryKey: [...nfKeys.query(`/netflow/exporter-devices/${enc}/impact`, { device_id: device.device_id })],
    queryFn: ({ signal }) => nf.get<MappingImpact>(`/netflow/exporter-devices/${enc}/impact`, { device_id: device.device_id }, { signal }),
    retry: nfRetry,
  })
  const noteId = useId()
  return (
    <section aria-labelledby="nf-map-review" className="space-y-3 rounded-md border border-primary/40 bg-primary/5 p-3">
      <h3 id="nf-map-review" className="text-sm font-semibold text-text">
        Link {ipDisplay(exporter)} to {device.hostname}?
      </h3>
      <QueryPanel query={impact} label="the effect of this link" skeleton="rows" compact>
        {(r) => <ImpactBody i={r.data} />}
      </QueryPanel>
      <div className="space-y-1">
        <label htmlFor={noteId} className="text-xs font-medium text-text2">Note (optional, kept in the history)</label>
        <Textarea id={noteId} value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)}
          placeholder="Why this exporter belongs to this device" className="text-xs" />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={onConfirm} disabled={!canManage || saving || impact.status !== 'success' || impact.data?.data.unchanged}>
          {saving && <Loader2 className="animate-spin" aria-hidden />}
          Confirm link to {device.hostname}
        </Button>
        <Button size="sm" variant="outline" onClick={onCancel} disabled={saving}>Choose another device</Button>
      </div>
    </section>
  )
}

function ImpactBody({ i }: { i: MappingImpact }) {
  const lines = useMemo(() => impactLines(i), [i])
  return (
    <div className="space-y-2 text-xs">
      <ul className="list-disc space-y-1 pl-5 text-text">
        {lines.map((l, k) => <li key={k}>{l}</li>)}
      </ul>
      {i.guidance.length > 0 && (
        <ul className="space-y-1">
          {i.guidance.map((g) => (
            <li key={g.code} className="flex flex-wrap items-center gap-2">
              <ToneChip tone={g.code === 'overrides_ip_match' ? 'info' : 'warning'} className="whitespace-normal">{g.message}</ToneChip>
              {(g.code === 'no_snmp' || g.code === 'no_interfaces' || g.code === 'no_speeds') && (
                <Link to={`/devices/${i.target.device_id}`} className="text-xs font-medium text-text underline underline-offset-2">Open the device</Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function HistoryList({ rows }: { rows: HistoryEntry[] }) {
  const tz = displayTimeZone()
  return (
    <ol className="space-y-1.5">
      {rows.map((h, k) => {
        const d = h.details as Record<string, any>
        let text: string
        if (h.kind === 'mapping') {
          text = h.action === 'delete'
            ? `Removed the link to ${d.previous_device_hostname ?? 'a device'}`
            : `Linked to ${d.device_hostname ?? 'a device'}` + (d.previous_device_hostname ? ` (was ${d.previous_device_hostname}${d.previous_source === 'ip-match' ? ', address match' : ''})` : '')
          if (d.note) text += ` — “${d.note}”`
        } else {
          const after = d.after as Record<string, any> | undefined
          text = h.action === 'delete'
            ? 'Settings reset to defaults'
            : `Settings: role ${after?.role ?? '—'}, counting ${after?.count_direction ?? '—'}` +
              (after?.expected_interval_s ? `, expected every ${after.expected_interval_s} s` : '') +
              (after?.display_name ? `, name “${after.display_name}”` : '')
        }
        return (
          <li key={k} className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
            <span className="shrink-0 tabular-nums text-text2">{formatDateTime(h.at, tz, { seconds: false })}</span>
            <span className="text-text"><span className="font-medium">{h.actor ?? 'system'}</span> · {text}</span>
          </li>
        )
      })}
    </ol>
  )
}

export { ExporterDeviceDialog }
