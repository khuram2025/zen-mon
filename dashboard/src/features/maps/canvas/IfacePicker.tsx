import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/* Shared port picker for the New link / Edit link dialogs.
 *
 * Stored port names come from several places — a person picking from this
 * list (the device's own if_name), Quick connect (CDP/LLDP neighbour port ids
 * such as "GigabitEthernet1/0/51") or older maps typed by hand. The select
 * matches a stored value by its normalised form so "GigabitEthernet1/0/51"
 * finds "Gi1/0/51", and a value that matches nothing stays selected as a
 * "not found" option instead of silently becoming "none" (which Save would
 * then write back, wiping the binding). */

export type DeviceInterface = {
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_speed?: number | null
  oper_status?: string | null
  admin_status?: string | null
  last_up_at?: string | null
  ever_up?: boolean | null
}

export const inputCls = 'w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60'

/** Same abbreviations as the backend (map_topology.norm_iface), longest names first so
 *  "FortyGigabitEthernet" is not half-replaced by the "GigabitEthernet" rule. */
const ABBREV: [string, string][] = [
  ['hundredgigabitethernet', 'hu'],
  ['fortygigabitethernet', 'fo'],
  ['twentyfivegigabitethernet', 'twe'],
  ['twentyfivegige', 'twe'],
  ['tengigabitethernet', 'te'],
  ['twogigabitethernet', 'tw'],
  ['fivegigabitethernet', 'fi'],
  ['gigabitethernet', 'gi'],
  ['fastethernet', 'fa'],
  ['hundredgige', 'hu'],
  ['fortygige', 'fo'],
  ['ethernet', 'eth'],
  ['port-channel', 'po'],
]

export function normIface(name: string | null | undefined): string {
  if (!name) return ''
  let n = name.trim().toLowerCase()
  for (const [full, short] of ABBREV) n = n.split(full).join(short)
  return n.replace(/\s+/g, '')
}

export function useInterfaces(deviceId: string | null | undefined) {
  return useQuery<DeviceInterface[]>({
    queryKey: ['device-interfaces', deviceId],
    enabled: !!deviceId,
    queryFn: async () => {
      const data = (await api.get(`/devices/${deviceId}/interfaces`)).data
      return Array.isArray(data) ? data : (data?.data || [])
    },
  })
}

const ifaceName = (i: DeviceInterface) => i.if_name || i.if_descr || `if${i.if_index}`

/** The interface a stored port name refers to, if any. */
export function findIface(ifaces: DeviceInterface[] | undefined, value: string | null | undefined): DeviceInterface | undefined {
  const needle = normIface(value)
  if (!needle || !ifaces) return undefined
  return ifaces.find((i) => [i.if_name, i.if_descr].some((v) => normIface(v) === needle))
}

function stateTag(i: DeviceInterface): string {
  const oper = (i.oper_status || '').toLowerCase()
  if (oper === 'up') return 'up'
  if (oper && !i.last_up_at && i.ever_up === false) return 'never up'
  return oper || '?'
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-medium text-muted">{label}</span>
      {children}
    </label>
  )
}

export function IfaceSelect({ label, loading, ifaces, value, onChange }: {
  label: string; loading: boolean; ifaces?: DeviceInterface[]; value: string; onChange: (v: string) => void
}) {
  const matched = useMemo(() => findIface(ifaces, value), [ifaces, value])
  // Show the device's own spelling for a matched value; keep an unmatched one.
  const selectValue = matched ? ifaceName(matched) : value
  const missing = !!value && !loading && !matched
  const sorted = useMemo(() => {
    const list = [...(ifaces || [])]
    // Up ports first, then by name — the port you want is almost always up.
    return list.sort((a, b) => Number((b.oper_status || '') === 'up') - Number((a.oper_status || '') === 'up') || ifaceName(a).localeCompare(ifaceName(b), undefined, { numeric: true }))
  }, [ifaces])
  return (
    <Field label={label}>
      <select className={inputCls} value={selectValue} onChange={(e) => onChange(e.target.value)} disabled={loading}>
        <option value="">{loading ? 'Loading…' : '— none —'}</option>
        {missing && <option value={value}>{value} (not found on device)</option>}
        {sorted.map((i) => {
          const name = ifaceName(i)
          const bits = [stateTag(i), speedLabel(i.if_speed) || null, i.if_alias].filter(Boolean)
          return <option key={i.if_index} value={name}>{name} · {bits.join(' · ')}</option>
        })}
      </select>
      {matched && (matched.oper_status || '').toLowerCase() !== 'up' && (
        <span className="text-[10px] text-warning">This port is {stateTag(matched)} — check the cabling or pick another port.</span>
      )}
      {missing && <span className="text-[10px] text-warning">Stored port "{value}" is not on this device — it is kept until you pick another.</span>}
    </Field>
  )
}

/** Free-text port label for ends that have no SNMP interfaces (servers, checks). */
export function PortText({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <Field label={label}>
      <input className={inputCls} value={value} onChange={(e) => onChange(e.target.value)} placeholder="e.g. NIC1 (optional)" />
    </Field>
  )
}

export const SPEEDS = ['', '10M', '100M', '1G', '2.5G', '10G', '25G', '40G', '100G'] as const

/** Speed label (as stored in link metadata) for an interface speed in bps. */
export function speedLabel(bps: number | null | undefined): string {
  if (!bps || bps <= 0) return ''
  if (bps >= 100e9) return '100G'
  if (bps >= 40e9) return '40G'
  if (bps >= 25e9) return '25G'
  if (bps >= 10e9) return '10G'
  if (bps >= 2.5e9) return '2.5G'
  if (bps >= 1e9) return '1G'
  if (bps >= 100e6) return '100M'
  return '10M'
}
