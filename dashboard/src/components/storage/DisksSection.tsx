import { useMemo, useState } from 'react'
import {
  AlertOctagon, AlertTriangle, ArrowRightLeft, ChevronDown, ChevronRight, HardDrive, Layers,
  Loader2, Maximize2, Plus, RefreshCw, Unplug, X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { GIB, allocationBytes, bytes, usageTextTone, diskName, diskStatus } from './storageMath'
import type { Disk, DiskStatus, Layout, PhysicalVolume, Volume } from './types'
import { useStorageAction, useStorageStatus } from './useStorage'

const STATUS_BADGE: Record<DiskStatus, 'success' | 'info' | 'warning' | 'danger' | 'default' | 'outline'> = {
  system: 'default',
  pool: 'success',
  unused: 'info',
  needs_wipe: 'warning',
  foreign: 'outline',
  in_use: 'outline',
  read_only: 'outline',
}

export function DisksSection() {
  const { data: status, isLoading, isError, refetch } = useStorageStatus()
  const [addTarget, setAddTarget] = useState<Disk | null>(null)
  const [extendOpen, setExtendOpen] = useState(false)
  const [claimTarget, setClaimTarget] = useState<{ pv: string; bytes: number } | null>(null)
  const [removeTarget, setRemoveTarget] = useState<PhysicalVolume | null>(null)

  const rescan = useStorageAction('/system/storage/rescan', { title: 'Disk rescan' })

  if (isError) return <Card><CardContent className="py-5 text-sm">Disk information is unavailable. <Button variant="ghost" onClick={() => refetch()}>Retry</Button></CardContent></Card>
  if (isLoading || !status) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 py-6 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> Reading disks and volumes…
        </CardContent>
      </Card>
    )
  }
  const layout = status.layout
  if (!layout.available) {
    return (
      <Card>
        <CardContent className="space-y-2 py-5">
          <div className="flex items-center gap-2 text-sm font-medium text-warning">
            <AlertTriangle className="h-4 w-4" /> Disk management is unavailable
          </div>
          <p className="text-xs text-text2">Refresh to retry. Contact support if disk management remains unavailable.</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <PoolCard
        layout={layout}
        onRescan={() => rescan.mutate()}
        rescanning={rescan.isPending}
        onExtend={() => setExtendOpen(true)}
        onClaim={(pv, b) => setClaimTarget({ pv, bytes: b })}
      />
      <DisksCard
        layout={layout}
        onAdd={setAddTarget}
        onClaim={(pv, b) => setClaimTarget({ pv, bytes: b })}
        onRemove={setRemoveTarget}
      />
      <ExpansionGuide dataSharesOs={layout.data_shares_os} />

      {addTarget && (
        <AddDiskDialog disk={addTarget} layout={layout} onClose={() => setAddTarget(null)} />
      )}
      {extendOpen && <ExtendDialog layout={layout} onClose={() => setExtendOpen(false)} />}
      {claimTarget && (
        <ClaimDialog target={claimTarget} layout={layout} onClose={() => setClaimTarget(null)} />
      )}
      {removeTarget && <RemoveDiskDialog layout={layout} pv={removeTarget} onClose={() => setRemoveTarget(null)} />}
    </div>
  )
}

// ─── Pool summary ───────────────────────────────────────────────────────────

function PoolCard({ layout, onRescan, rescanning, onExtend, onClaim }: {
  layout: Layout
  onRescan: () => void
  rescanning: boolean
  onExtend: () => void
  onClaim: (pv: string, bytes: number) => void
}) {
  const vg = layout.volume_groups.find((v) => v.is_data)
  const abort = useStorageAction('/system/storage/evacuate/abort', { title: 'Abort data move' })
  const forget = useStorageAction('/system/storage/remove-missing', { title: 'Forget missing disk' })
  const allocated = vg ? vg.size_bytes - vg.free_bytes : 0
  const canAllocate = !layout.pvmove && layout.unallocated_bytes >= Math.max(layout.extent_bytes, 1)

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Layers className="h-4 w-4 text-primary" /> Storage pool
            </CardTitle>
            <p className="mt-1 text-xs text-muted">
              Review connected disks, make added capacity available and allocate it to the appliance.
              Supported expansion applies online while monitoring continues.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={onRescan} disabled={rescanning}>
              {rescanning ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              Rescan disks
            </Button>
            <Button size="sm" onClick={onExtend} disabled={!canAllocate}
              title={canAllocate ? undefined : 'No unallocated space in the pool'}>
              <Maximize2 /> Allocate space
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {layout.missing_pvs.map((m) => (
          <div key={m.uuid} className={cn('flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2.5',
            m.used_bytes > 0 ? 'border-danger/40 bg-danger/10' : 'border-warning/40 bg-warning/10')}>
            <AlertOctagon className={cn('h-4 w-4 shrink-0', m.used_bytes > 0 ? 'text-danger' : 'text-warning')} />
            <div className="min-w-0 flex-1 text-sm">
              <div className="font-medium">A {bytes(m.size_bytes)} disk is missing from the pool</div>
              <div className="text-xs text-text2">
                {m.used_bytes > 0
                  ? `It held ${bytes(m.used_bytes)} of data. Reattach the disk to the VM, then rescan.`
                  : 'It held no data and can be forgotten safely.'}
              </div>
            </div>
            {m.used_bytes === 0 && (
              <Button size="sm" variant="outline" onClick={() => forget.mutate()} disabled={forget.isPending}>
                {forget.isPending && <Loader2 className="animate-spin" />} Forget disk
              </Button>
            )}
          </div>
        ))}

        {layout.pvmove && (
          <div className="rounded-lg border border-info/30 bg-info/5 px-3 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="flex items-center gap-2 font-medium text-info">
                <ArrowRightLeft className="h-4 w-4" /> Moving data off {diskName(layout, layout.pvmove.pv)}
              </span>
              <span className="flex items-center gap-3">
                <span className="tabular-nums text-text2">{layout.pvmove.percent.toFixed(1)}%</span>
                <Button size="sm" variant="ghost" onClick={() => abort.mutate()} disabled={abort.isPending}>
                  <X /> Abort
                </Button>
              </span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface2">
              <div className="h-full bg-info transition-all" style={{ width: `${layout.pvmove.percent}%` }} />
            </div>
            <p className="mt-1.5 text-[11px] text-muted">
              When the move completes, remove the disk from the pool, then detach it from the VM.
            </p>
          </div>
        )}

        {layout.claimable.map((c) => (
          <div key={c.pv} className="flex flex-wrap items-center gap-3 rounded-lg border border-info/30 bg-info/5 px-3 py-2.5">
            <Maximize2 className="h-4 w-4 shrink-0 text-info" />
            <div className="min-w-0 flex-1 text-sm">
              <span className="font-medium">{bytes(c.bytes)}</span> of new space on{' '}
              <span className="font-mono">{diskName(layout, c.pv)}</span>
              <div className="text-xs text-text2">The virtual disk was enlarged. Claim the space to use it.</div>
            </div>
            <Button size="sm" onClick={() => onClaim(c.pv, c.bytes)} disabled={!!layout.pvmove}>
              Claim space
            </Button>
          </div>
        ))}

        <div className="grid gap-3 sm:grid-cols-4">
          <Stat label="Pool capacity" value={bytes(vg?.size_bytes || 0)}
            sub={`${vg?.pv_count || 0} disk${vg?.pv_count === 1 ? '' : 's'}`} />
          <Stat label="Allocated" value={bytes(allocated)} sub="Assigned to volumes" />
          <Stat label="Unallocated" value={bytes(layout.unallocated_bytes)}
            sub={layout.unallocated_bytes > 0 ? 'Reserve, ready to allocate' : 'Fully allocated'}
            tone={layout.unallocated_bytes >= GIB ? 'info' : undefined} />
          <Stat label="Claimable" value={bytes(layout.claimable.reduce((a, c) => a + c.bytes, 0))}
            sub="From enlarged disks" tone={layout.claimable.length ? 'info' : undefined} />
        </div>

        {vg && vg.size_bytes > 0 && (
          <div>
            <div className="flex h-2.5 overflow-hidden rounded-full bg-surface2" role="img"
              aria-label={`Pool ${bytes(allocated)} allocated of ${bytes(vg.size_bytes)}`}>
              <div className="h-full bg-primary" style={{ width: `${(allocated / vg.size_bytes) * 100}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-[11px] text-muted">
              <span>Allocated {bytes(allocated)}</span>
              <span>Unallocated {bytes(vg.free_bytes)}</span>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {layout.volumes.map((v) => <VolumeRow key={v.path} volume={v} />)}
        </div>
      </CardContent>
    </Card>
  )
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: 'info' }) {
  return (
    <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={cn('mt-0.5 text-lg font-semibold tabular-nums', tone === 'info' && 'text-info')}>{value}</div>
      <div className="text-[11px] text-muted">{sub}</div>
    </div>
  )
}

function VolumeRow({ volume }: { volume: Volume }) {
  const pct = volume.usage?.usage_percent ?? 0
  const roles = volume.roles.includes('data') && volume.roles.includes('system')
    ? 'System & data' : volume.roles.includes('data') ? 'Data' : 'System'
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border/50 px-3 py-2 text-sm">
      <div className="min-w-[12rem] flex-1">
        <div className="font-medium">{roles} volume</div>
        <div className="text-[11px] text-muted">{bytes(volume.usage?.free_bytes || 0)} available
        </div>
      </div>
      <div className="w-40">
        <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
          <div className={cn('h-full', pct >= 95 ? 'bg-danger' : pct >= 85 ? 'bg-warning' : 'bg-primary')}
            style={{ width: `${Math.min(100, pct)}%` }} />
        </div>
      </div>
      <div className="text-right text-xs tabular-nums text-text2">
        <span className={cn('font-semibold', usageTextTone(pct))}>{pct.toFixed(1)}%</span>{' '}
        of {bytes(volume.size_bytes || volume.usage?.total_bytes || 0)}
      </div>
    </div>
  )
}

// ─── Disks ──────────────────────────────────────────────────────────────────

function DisksCard({ layout, onAdd, onClaim, onRemove }: {
  layout: Layout
  onAdd: (d: Disk) => void
  onClaim: (pv: string, bytes: number) => void
  onRemove: (pv: PhysicalVolume) => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const busy = !!layout.pvmove
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="h-4 w-4 text-primary" /> Disks
        </CardTitle>
        <p className="text-xs text-muted">
          Every disk attached to the appliance. New disks appear after a rescan.
        </p>
      </CardHeader>
      <CardContent className="space-y-2">
        {layout.disks.length === 0 && (
          <div className="rounded-lg border border-dashed border-border/60 p-4 text-center text-xs text-muted">
            No disks detected.
          </div>
        )}
        {layout.disks.map((d) => {
          const poolPvs = d.pvs.filter((p) => p.in_data_vg)
          const expanded = open === d.name
          const removable = poolPvs.find((p) => p.removal.action)
          return (
            <div key={d.name} className="rounded-lg border border-border/60">
              <div className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                <button type="button" className="flex min-w-[14rem] flex-1 items-center gap-3 text-left"
                  onClick={() => setOpen(expanded ? null : d.name)} aria-expanded={expanded}>
                  {expanded ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}
                  <HardDrive className="h-5 w-5 shrink-0 text-text2" />
                  <span>
                    <span className="font-mono text-sm font-medium">{diskName(layout, d.path)}</span>{' '}
                    <span className="ml-2 text-sm tabular-nums">{bytes(d.size_bytes)}</span>
                    <span className="block text-[11px] text-muted">
                      {[d.model, d.transport?.toUpperCase(), d.rotational ? 'HDD' : 'SSD/virtual'].filter(Boolean).join(' · ')}
                    </span>
                  </span>
                </button>
                <Badge variant={STATUS_BADGE[d.status]}>{diskStatus[d.status] || 'Unavailable'}</Badge>
                {poolPvs.some((p) => p.claimable_bytes > 0) && (
                  <Badge variant="info">+{bytes(poolPvs.reduce((a, p) => a + p.claimable_bytes, 0))} claimable</Badge>
                )}
                <div className="flex gap-2">
                  {d.can_add && (
                    <Button size="sm" onClick={() => onAdd(d)} disabled={busy}>
                      <Plus /> Add to pool
                    </Button>
                  )}
                  {poolPvs.filter((p) => p.claimable_bytes > 0).map((p) => (
                    <Button key={p.path} size="sm" variant="outline" onClick={() => onClaim(p.path, p.claimable_bytes)} disabled={busy}>
                      Claim
                    </Button>
                  ))}
                  {removable && (
                    <Button size="sm" variant="outline" onClick={() => onRemove(removable)} disabled={busy}>
                      <Unplug /> Remove
                    </Button>
                  )}
                </div>
              </div>
              {poolPvs.map((p) => <PvUsage key={p.path} pv={p} />)}
              {expanded && <DiskDetail disk={d} />}
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

function PvUsage({ pv }: { pv: PhysicalVolume }) {
  const pct = pv.size_bytes ? (pv.used_bytes / pv.size_bytes) * 100 : 0
  return (
    <div className="border-t border-border/40 px-3 py-2 pl-12 text-xs">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-mono text-text2">Allocated capacity</span>
        <div className="h-1.5 min-w-[8rem] flex-1 overflow-hidden rounded-full bg-surface2">
          <div className="h-full bg-primary" style={{ width: `${pct}%` }} />
        </div>
        <span className="tabular-nums text-text2">
          {bytes(pv.used_bytes)} allocated · {bytes(pv.free_bytes)} free in pool
        </span>
      </div>
      {!pv.removal.action && pv.removal.reason && (
        <div className="mt-1 text-[11px] text-muted">This disk cannot currently be removed safely. Keep it connected.</div>
      )}
    </div>
  )
}

function DiskDetail({ disk }: { disk: Disk }) {
  return (
    <div className="space-y-2 border-t border-border/40 bg-surface2/20 px-3 py-2.5 pl-12 text-xs">
      <p className="text-text2">{diskStatus[disk.status] || 'Status unavailable'}. {disk.needs_wipe ? 'Existing contents must be erased before adding this disk.' : 'Review disk identity before making changes.'}</p>
      {disk.model && <div className="text-muted">Model: {disk.model}</div>}
      {disk.serial && <div className="text-muted">Serial: <span className="font-mono">{disk.serial}</span></div>}
      <div className="text-muted">Capacity: {bytes(disk.size_bytes)}</div>
    </div>
  )
}

// ─── Guidance ───────────────────────────────────────────────────────────────

function ExpansionGuide({ dataSharesOs }: { dataSharesOs: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <Card>
      <button type="button" className="flex w-full items-center gap-2 px-5 py-3.5 text-left text-sm font-medium"
        onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}
        Expand capacity or safely remove a disk
      </button>
      {open && (
        <CardContent className="grid gap-4 text-xs text-text2 md:grid-cols-3">
          <div>
            <div className="mb-1 font-semibold text-text">Add a new disk</div>
            <ol className="list-decimal space-y-0.5 pl-4">
              <li>Attach a new virtual disk to the VM in your hypervisor.</li>
              <li>Click <em>Rescan disks</em>.</li>
              <li>Click <em>Add to pool</em> and choose how much to allocate.</li>
            </ol>
          </div>
          <div>
            <div className="mb-1 font-semibold text-text">Enlarge an existing disk</div>
            <ol className="list-decimal space-y-0.5 pl-4">
              <li>Increase the disk size in the hypervisor.</li>
              <li>Click <em>Rescan disks</em>, then <em>Claim space</em>.</li>
              <li>Allocate the new space to the {dataSharesOs ? 'system & data' : 'data'} volume.</li>
            </ol>
          </div>
          <div>
            <div className="mb-1 font-semibold text-text">Remove a disk</div>
            <ol className="list-decimal space-y-0.5 pl-4">
              <li>Ensure the other disks have enough unallocated space for its data (keep added space unallocated when planning to remove a disk).</li>
              <li>Click <em>Remove</em>: data moves off online, then the disk leaves the pool.</li>
              <li>Detach the disk from the VM only after it shows <em>Unused</em>.</li>
            </ol>
          </div>
        </CardContent>
      )}
    </Card>
  )
}

// ─── Dialogs ────────────────────────────────────────────────────────────────

type AllocationMode = 'all' | 'custom' | 'none'

function AllocationPicker({ mode, setMode, gib, setGib, maxBytes, dataSharesOs }: {
  mode: AllocationMode
  setMode: (m: AllocationMode) => void
  gib: string
  setGib: (v: string) => void
  maxBytes: number
  dataSharesOs: boolean
}) {
  const maxGib = Math.floor(maxBytes / GIB)
  const target = dataSharesOs ? 'system & data volume' : 'data volume'
  const options: { id: AllocationMode; title: string; body: string }[] = [
    { id: 'all', title: `Allocate all to the ${target}`, body: 'Recommended. The volume grows online immediately.' },
    { id: 'custom', title: 'Allocate part of it', body: 'Keep the rest as unallocated reserve for later growth or disk removal.' },
    { id: 'none', title: 'Keep unallocated', body: 'Add capacity to the pool now and allocate it later.' },
  ]
  return (
    <div className="space-y-2" role="radiogroup" aria-label="Allocation">
      {options.map((o) => (
        <label key={o.id} className={cn('flex cursor-pointer gap-3 rounded-lg border px-3 py-2',
          mode === o.id ? 'border-primary/60 bg-primary/5' : 'border-border/60 hover:bg-surface2/40')}>
          <input type="radio" name="allocation" className="mt-1 accent-[var(--primary,#3b82f6)]"
            checked={mode === o.id} onChange={() => setMode(o.id)} />
          <span className="text-sm">
            <span className="font-medium">{o.title}</span>
            <span className="block text-xs text-muted">{o.body}</span>
          </span>
        </label>
      ))}
      {mode === 'custom' && (
        <div className="flex items-center gap-3 pl-8">
          <input type="range" min={1} max={Math.max(1, maxGib)} value={Math.min(Number(gib) || 1, Math.max(1, maxGib))}
            onChange={(e) => setGib(e.target.value)} className="flex-1 accent-[var(--primary,#3b82f6)]"
            aria-label="Amount to allocate in GiB" />
          <Input type="number" min={1} max={maxGib} value={gib} onChange={(e) => setGib(e.target.value)}
            className="h-8 w-24 text-right tabular-nums" aria-label="GiB to allocate" />
          <span className="text-xs text-muted">of {maxGib} GiB</span>
        </div>
      )}
    </div>
  )
}

function AddDiskDialog({ disk, layout, onClose }: { disk: Disk; layout: Layout; onClose: () => void }) {
  const [mode, setMode] = useState<AllocationMode>('all')
  const [gib, setGib] = useState(String(Math.max(1, Math.floor(disk.size_bytes / GIB / 2))))
  const [confirm, setConfirm] = useState('')
  const add = useStorageAction<{ disk: string; allocate: string; wipe: boolean; confirm?: string }>(
    '/system/storage/add-disk', { title: 'Add disk', onDone: onClose },
  )
  // Free space after the disk joins: what is unallocated now plus the disk.
  const poolFreeAfter = layout.unallocated_bytes + disk.size_bytes
  const custom = allocationBytes(gib, layout.extent_bytes, poolFreeAfter)
  const name = diskName(layout, disk.path)
  const valid = (mode !== 'custom' || custom != null) && (!disk.needs_wipe || confirm === name)

  function submit() {
    const allocate = mode === 'all' ? 'all' : mode === 'none' ? 'none' : String(custom)
    add.mutate({ disk: disk.path, allocate, wipe: disk.needs_wipe, confirm: disk.needs_wipe ? disk.path : undefined })
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !add.isPending && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Plus className="h-5 w-5 text-primary" /> Add {name} to the storage pool</DialogTitle>
        </DialogHeader>
        <DialogDescription>
          {bytes(disk.size_bytes)}{disk.model ? ` · ${disk.model}` : ''}. This disk will be prepared and added to appliance storage.
          {disk.serial && <> Serial: {disk.serial}.</>}
        </DialogDescription>
        {disk.needs_wipe && (
          <div className="space-y-2 rounded-lg border border-danger/40 bg-danger/10 p-3 text-sm">
            <div className="flex items-center gap-2 font-medium text-danger">
              <AlertTriangle className="h-4 w-4" /> Everything on this disk will be erased
            </div>
            <p className="text-xs text-text2">Check the disk model, serial and capacity above. Existing contents will be permanently lost.</p>
            <label className="block text-xs text-text2">
              Type <span className="font-mono font-semibold text-text">{name}</span> to confirm
              <Input className="mt-1 h-8 font-mono" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                autoComplete="off" spellCheck={false} />
            </label>
          </div>
        )}
        <AllocationPicker mode={mode} setMode={setMode} gib={gib} setGib={setGib}
          maxBytes={poolFreeAfter} dataSharesOs={layout.data_shares_os} />
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={add.isPending}>Cancel</Button>
          <Button variant={disk.needs_wipe ? 'destructive' : 'default'} onClick={submit} disabled={!valid || add.isPending}>
            {add.isPending && <Loader2 className="animate-spin" />}
            {disk.needs_wipe ? 'Erase and add disk' : 'Add disk'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ExtendDialog({ layout, onClose }: { layout: Layout; onClose: () => void }) {
  const targets = useMemo(
    () => layout.volumes.filter((v) => v.on_lvm && v.vg === layout.data_vg),
    [layout],
  )
  const [mount, setMount] = useState(targets.find((v) => v.roles.includes('data'))?.mount_point || '/data')
  const [mode, setMode] = useState<'all' | 'custom'>('all')
  const [gib, setGib] = useState(String(Math.max(1, Math.floor(layout.unallocated_bytes / GIB / 2))))
  const extend = useStorageAction<{ mount: string; size_bytes: number | null }>(
    '/system/storage/extend', { title: 'Allocate space', onDone: onClose },
  )
  const custom = allocationBytes(gib, layout.extent_bytes, layout.unallocated_bytes)
  const apiMount = targets.find((v) => v.mount_point === mount)?.roles.includes('data') ? '/data' : '/'

  return (
    <Dialog open onOpenChange={(o) => !o && !extend.isPending && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Maximize2 className="h-5 w-5 text-primary" /> Allocate pool space</DialogTitle>
        </DialogHeader>
        <DialogDescription>
          {bytes(layout.unallocated_bytes)} is unallocated. The selected capacity grows online.
          Allocated space cannot be returned to the pool without downtime, so allocate what you need.
        </DialogDescription>
        {targets.length > 1 && (
          <div className="space-y-1.5">
            {targets.map((v) => (
              <label key={v.path} className="flex items-center gap-2 text-sm">
                <input type="radio" name="target" checked={mount === v.mount_point}
                  onChange={() => setMount(v.mount_point)} className="accent-[var(--primary,#3b82f6)]" />
                {v.roles.includes('data') ? 'Data volume' : 'System volume'}
                <span className="text-xs text-muted">{bytes(v.size_bytes)} currently</span>
              </label>
            ))}
          </div>
        )}
        <div className="space-y-2" role="radiogroup" aria-label="Amount">
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" checked={mode === 'all'} onChange={() => setMode('all')} className="accent-[var(--primary,#3b82f6)]" />
            All unallocated space ({bytes(layout.unallocated_bytes)})
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" checked={mode === 'custom'} onChange={() => setMode('custom')} className="accent-[var(--primary,#3b82f6)]" />
            A specific amount
          </label>
          {mode === 'custom' && (
            <div className="flex items-center gap-3 pl-6">
              <input type="range" min={1} max={Math.max(1, Math.floor(layout.unallocated_bytes / GIB))}
                value={Number(gib) || 1} onChange={(e) => setGib(e.target.value)}
                className="flex-1 accent-[var(--primary,#3b82f6)]" aria-label="GiB to allocate" />
              <Input type="number" min={1} value={gib} onChange={(e) => setGib(e.target.value)}
                className="h-8 w-24 text-right tabular-nums" aria-label="GiB" />
              <span className="text-xs text-muted">GiB</span>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={extend.isPending}>Cancel</Button>
          <Button onClick={() => extend.mutate({ mount: apiMount, size_bytes: mode === 'all' ? null : custom })}
            disabled={extend.isPending || (mode === 'custom' && custom == null)}>
            {extend.isPending && <Loader2 className="animate-spin" />}
            Allocate {mode === 'all' ? bytes(layout.unallocated_bytes) : custom ? bytes(custom) : ''}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ClaimDialog({ target, layout, onClose }: {
  target: { pv: string; bytes: number }
  layout: Layout
  onClose: () => void
}) {
  const [allocate, setAllocate] = useState(true)
  const [pending, setPending] = useState(false)
  const claim = useStorageAction<{ pv: string }>('/system/storage/claim', { title: 'Claim space' })
  const extend = useStorageAction<{ mount: string; size_bytes: null }>('/system/storage/extend', { title: 'Allocate space' })

  async function submit() {
    setPending(true)
    try {
      await claim.mutateAsync({ pv: target.pv })
      if (allocate) await extend.mutateAsync({ mount: '/data', size_bytes: null })
      onClose()
    } catch {
      // Errors are toasted by the mutation hooks.
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Maximize2 className="h-5 w-5 text-primary" /> Claim new disk space</DialogTitle>
        </DialogHeader>
        <DialogDescription>
          Makes the additional {bytes(target.bytes)} on {diskName(layout, target.pv)} available to this appliance. Existing data is preserved.
        </DialogDescription>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={allocate} onChange={(e) => setAllocate(e.target.checked)}
            className="mt-0.5 h-4 w-4 accent-[var(--primary,#3b82f6)]" />
          <span>
            Allocate it to the {layout.data_shares_os ? 'system & data' : 'data'} volume right away
            <span className="block text-xs text-muted">Leave unchecked to keep it as unallocated reserve.</span>
          </span>
        </label>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={pending}>Cancel</Button>
          <Button onClick={submit} disabled={pending}>
            {pending && <Loader2 className="animate-spin" />} Claim {bytes(target.bytes)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RemoveDiskDialog({ pv, layout, onClose }: { pv: PhysicalVolume; layout: Layout; onClose: () => void }) {
  const name = diskName(layout, pv.path)
  const [confirm, setConfirm] = useState('')
  const evacuate = pv.removal.action === 'evacuate'
  const action = useStorageAction<{ pv: string; confirm: string }>(
    evacuate ? '/system/storage/evacuate' : '/system/storage/remove-disk',
    { title: evacuate ? 'Move data off disk' : 'Remove disk', onDone: onClose },
  )
  return (
    <Dialog open onOpenChange={(o) => !o && !action.isPending && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Unplug className="h-5 w-5 text-warning" /> {evacuate ? 'Move data off' : 'Remove'} {name}
          </DialogTitle>
        </DialogHeader>
        <DialogDescription>
          {evacuate ? (
            <>
              Step 1 of 2. Move {bytes(pv.used_bytes)} to other disks before removal. This can take a while; progress shows on
              this page. When it finishes, click <em>Remove</em> again to take the disk out of the pool.
            </>
          ) : (
            <>
              The disk holds no allocated data. Available pool capacity decreases by {bytes(pv.size_bytes)}. Detach it from the VM afterwards.
            </>
          )}
        </DialogDescription>
        <label className="block text-xs text-text2">
          Type <span className="font-semibold text-text">{name}</span> to confirm
          <Input className="mt-1 h-8 font-mono" value={confirm} onChange={(e) => setConfirm(e.target.value)}
            autoComplete="off" spellCheck={false} />
        </label>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={action.isPending}>Cancel</Button>
          <Button variant="destructive" disabled={confirm !== name || action.isPending}
            onClick={() => action.mutate({ pv: pv.path, confirm: pv.path })}>
            {action.isPending && <Loader2 className="animate-spin" />}
            {evacuate ? 'Start moving data' : 'Remove from pool'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
