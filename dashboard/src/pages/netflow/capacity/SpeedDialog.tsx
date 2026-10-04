import { useEffect, useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Gauge, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { cn } from '@/lib/utils'
import { formatBps, ipDisplay } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import {
  overrideFormNote,
  overrideShadowNote,
  parseSpeed,
  ROLE_LABELS,
  ROLES,
  SOURCE_LABELS,
  speedInput,
  speedTargets,
  SPEED_UNITS,
  type CapacityPermissions,
  type CapacityRow,
  type InterfaceRole,
  type SpeedUnit,
} from './model'
import { selectCls } from './shared'

type Target = 'device' | 'netflow'

function SpeedField({
  label,
  value,
  unit,
  onValue,
  onUnit,
  error,
  hint,
}: {
  label: string
  value: string
  unit: SpeedUnit
  onValue: (v: string) => void
  onUnit: (u: SpeedUnit) => void
  error?: string | null
  hint?: string
}) {
  const id = useId()
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-xs font-medium text-text">{label}</label>
      <div className="flex gap-2">
        <Input
          id={id}
          inputMode="decimal"
          value={value}
          onChange={(e) => onValue(e.target.value)}
          aria-invalid={!!error}
          aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined}
          placeholder="e.g. 100"
          className="max-w-[10rem]"
        />
        <select aria-label={`${label} unit`} className={selectCls} value={unit} onChange={(e) => onUnit(e.target.value as SpeedUnit)}>
          {Object.keys(SPEED_UNITS).map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
      </div>
      {error ? (
        <p id={`${id}-err`} className="text-[11px] text-danger-fg">{error}</p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-[11px] text-text2">{hint}</p>
      ) : null}
    </div>
  )
}

/**
 * Audited speed changes (NF-16): the device interface's configured circuit speed (shared with
 * link utilization and interface alerts) for inventoried interfaces, or a NetFlow interface
 * override (per-direction speed, name, role) for anything else.
 */
export function SpeedDialog({
  row,
  perms,
  open,
  onOpenChange,
}: {
  row: CapacityRow | null
  perms: CapacityPermissions
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const qc = useQueryClient()
  const targets = row ? speedTargets(row, perms) : []
  const [target, setTarget] = useState<Target>('device')
  const [dev, setDev] = useState({ value: '', unit: 'Mbps' as SpeedUnit })
  const [inS, setInS] = useState({ value: '', unit: 'Mbps' as SpeedUnit })
  const [outS, setOutS] = useState({ value: '', unit: 'Mbps' as SpeedUnit })
  const [name, setName] = useState('')
  const [role, setRole] = useState<InterfaceRole | ''>('')
  const [note, setNote] = useState('')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    if (!row || !open) return
    setTarget(targets[0] ?? 'device')
    setDev(speedInput(row.speed.configured_bps))
    setInS(speedInput(row.override?.speed_in_bps))
    setOutS(speedInput(row.override?.speed_out_bps))
    setName(row.override?.display_name ?? '')
    setRole(row.override?.role ?? '')
    setNote(row.override?.note ?? '')
    setTouched(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.key, open])

  const devParsed = parseSpeed(dev.value, dev.unit)
  const inParsed = parseSpeed(inS.value, inS.unit)
  const outParsed = parseSpeed(outS.value, outS.unit)
  const devError = touched && typeof devParsed === 'string' ? devParsed : null
  const inError = touched && typeof inParsed === 'string' ? inParsed : null
  const outError = touched && typeof outParsed === 'string' ? outParsed : null

  const done = (title: string, body: string) => {
    toast.success(title, body)
    qc.invalidateQueries({ queryKey: ['netflow'] })
    onOpenChange(false)
  }

  const saveDevice = useMutation({
    mutationFn: async () =>
      api.patch(`/devices/${row!.device_id}/interfaces/${row!.ifindex}`, { configured_speed_bps: devParsed as number | null }),
    onSuccess: () => done('Circuit speed saved', 'Capacity, link utilization and interface alerts use it from now on.'),
    onError: (e) => toast.error('Could not save the speed', apiErrorMessage(e)),
  })
  const saveOverride = useMutation({
    mutationFn: async () =>
      api.put(`/netflow/capacity/overrides/${encodeURIComponent(row!.exporter)}/${row!.ifindex}`, {
        display_name: name.trim() || null,
        speed_in_bps: inParsed as number | null,
        speed_out_bps: outParsed as number | null,
        role: role || null,
        note: note.trim() || null,
      }),
    onSuccess: () =>
      done(
        'Interface override saved',
        row?.speed.configured_bps && (inParsed != null || outParsed != null)
          ? `Recorded in the audit log. Its speeds are not in use while the device circuit speed (${formatBps(row.speed.configured_bps)}) is set.`
          : 'The change is recorded in the audit log.',
      ),
    onError: (e) => toast.error('Could not save the override', apiErrorMessage(e)),
  })
  const removeOverride = useMutation({
    mutationFn: async () => api.delete(`/netflow/capacity/overrides/${encodeURIComponent(row!.exporter)}/${row!.ifindex}`),
    onSuccess: () => done('Override removed', 'The interface falls back to the device and SNMP speeds.'),
    onError: (e) => toast.error('Could not remove the override', apiErrorMessage(e)),
  })

  if (!row) return null
  const pending = saveDevice.isPending || saveOverride.isPending || removeOverride.isPending
  const submit = () => {
    setTouched(true)
    if (target === 'device') {
      if (typeof devParsed === 'string') return
      saveDevice.mutate()
    } else {
      if (typeof inParsed === 'string' || typeof outParsed === 'string') return
      saveOverride.mutate()
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Gauge className="h-4 w-4 text-primary" aria-hidden />
            Interface capacity
          </DialogTitle>
          <DialogDescription>
            {row.display_name} · {row.device_hostname ?? 'no device'} · {ipDisplay(row.exporter)} ifIndex {row.ifindex}
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-2 gap-2 rounded-md border border-border bg-surface2/40 p-3 text-xs">
          <dt className="text-text2">In speed used</dt>
          <dd className="text-text">{row.speed.in_bps ? `${formatBps(row.speed.in_bps)} (${SOURCE_LABELS[row.speed.source_in]})` : 'Unknown'}</dd>
          <dt className="text-text2">Out speed used</dt>
          <dd className="text-text">{row.speed.out_bps ? `${formatBps(row.speed.out_bps)} (${SOURCE_LABELS[row.speed.source_out]})` : 'Unknown'}</dd>
          <dt className="text-text2">SNMP ifSpeed</dt>
          <dd className="text-text">{row.speed.snmp_bps ? formatBps(row.speed.snmp_bps) : row.interface_in_inventory ? 'Not reported' : 'Not polled'}</dd>
          {(row.speed.override_in_bps || row.speed.override_out_bps) && (
            <>
              <dt className="text-text2">NetFlow override</dt>
              <dd className="text-text">
                in {row.speed.override_in_bps ? formatBps(row.speed.override_in_bps) : 'not set'}, out{' '}
                {row.speed.override_out_bps ? formatBps(row.speed.override_out_bps) : 'not set'}
                {overrideShadowNote(row.speed) && <span className="block text-warning-fg">Not in use: the circuit speed takes precedence</span>}
              </dd>
            </>
          )}
        </dl>
        <p className="text-[11px] text-text2">
          Order used: configured circuit speed, then NetFlow override, then SNMP (or sFlow) ifSpeed.
        </p>

        {targets.length === 0 ? (
          <p role="status" className="rounded-md border border-border bg-surface2/40 p-3 text-xs text-text">
            Changing speeds needs the NetFlow manage permission (overrides) or device management (circuit speed). Ask an
            administrator.
          </p>
        ) : (
          <>
            {targets.length > 1 && (
              <div role="radiogroup" aria-label="Where to set the speed" className="grid grid-cols-1 gap-2 min-[480px]:grid-cols-2">
                {targets.map((t) => (
                  <button
                    key={t}
                    type="button"
                    role="radio"
                    aria-checked={target === t}
                    onClick={() => setTarget(t)}
                    className={cn(
                      'rounded-md border p-2 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                      target === t ? 'border-primary bg-primary/10' : 'border-border bg-surface hover:bg-surface2',
                    )}
                  >
                    <span className="block font-semibold text-text">{t === 'device' ? 'Device circuit speed' : 'NetFlow override'}</span>
                    <span className="text-text2">
                      {t === 'device' ? 'One speed for both directions, shared with link utilization and alerts' : 'Per-direction speed, name and role for this flow interface'}
                    </span>
                  </button>
                ))}
              </div>
            )}
            {target === 'device' ? (
              <SpeedField
                label="Circuit speed (both directions)"
                value={dev.value}
                unit={dev.unit}
                onValue={(v) => setDev((s) => ({ ...s, value: v }))}
                onUnit={(u) => setDev((s) => ({ ...s, unit: u }))}
                error={devError}
                hint="Leave empty to clear it and use the SNMP speed again."
              />
            ) : (
              <div className="space-y-3">
                {overrideFormNote(row.speed.configured_bps) && (
                  <p role="note" className="rounded-md border border-warning/35 bg-warning/10 p-2 text-xs text-text">
                    {overrideFormNote(row.speed.configured_bps)}
                  </p>
                )}
                <div className="grid grid-cols-1 gap-3 min-[480px]:grid-cols-2">
                  <SpeedField label="In speed (received)" value={inS.value} unit={inS.unit}
                    onValue={(v) => setInS((s) => ({ ...s, value: v }))} onUnit={(u) => setInS((s) => ({ ...s, unit: u }))} error={inError} />
                  <SpeedField label="Out speed (transmitted)" value={outS.value} unit={outS.unit}
                    onValue={(v) => setOutS((s) => ({ ...s, value: v }))} onUnit={(u) => setOutS((s) => ({ ...s, unit: u }))} error={outError} />
                </div>
                <div className="grid grid-cols-1 gap-3 min-[480px]:grid-cols-2">
                  <label className="space-y-1 text-xs font-medium text-text">
                    Display name
                    <Input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder={row.if_name ?? `ifIndex ${row.ifindex}`} />
                  </label>
                  <label className="space-y-1 text-xs font-medium text-text">
                    Role
                    <select className={cn(selectCls, 'w-full')} value={role} onChange={(e) => setRole(e.target.value as InterfaceRole | '')}>
                      <option value="">Not set</option>
                      {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}{r === 'ignore' ? ' (hide from capacity)' : ''}</option>)}
                    </select>
                  </label>
                </div>
                <label className="block space-y-1 text-xs font-medium text-text">
                  Note
                  <Textarea value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} placeholder="Circuit ID, provider, contract" />
                </label>
              </div>
            )}
          </>
        )}

        <DialogFooter className="gap-2">
          {target === 'netflow' && row.override && perms.manage && (
            <Button variant="outline" size="sm" className="mr-auto" disabled={pending} onClick={() => removeOverride.mutate()}>
              <Trash2 aria-hidden />
              Remove override
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
          {targets.length > 0 && (
            <Button size="sm" disabled={pending} onClick={submit}>
              {pending ? 'Saving…' : 'Save'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
