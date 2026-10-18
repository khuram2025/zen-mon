import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { TemplateInsightsSection, type ChartRequest, type InsightGroup } from '@/components/devices/TemplateInsightsSection'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Panel } from './charts'
import { MISSING } from './format'
import { isHardwareGroup } from './tabs'
import type { DeviceRecord, DeviceRange } from './types'

/* Physical health: the template's hardware groups (sensors with units and
 * thresholds, PSU, fans, temperature, stack members, PoE budget, memory pools
 * with used % bars, disks, optics) and the ENTITY-MIB component inventory. */

type Entity = {
  id?: string; ent_index?: number; parent_index?: number | null; class?: string | null; name?: string | null
  model_name?: string | null; serial_number?: string | null; hw_revision?: string | null; fw_revision?: string | null
}

const hardwareOnly = (g: InsightGroup) => isHardwareGroup(g)

export function HardwareTab({ device, range, onChart, initialGroup }: {
  device: DeviceRecord; range: DeviceRange; onChart: (r: ChartRequest) => void; initialGroup?: string
}) {
  const { data: entities, isPending, isError } = useQuery<Entity[]>({
    queryKey: ['device', device.id, 'entities'],
    queryFn: async () => (await api.get(`/devices/${device.id}/entities`)).data,
    placeholderData: keepPreviousData,
  })
  return (
    <section aria-label="Device hardware" className="space-y-4">
      {device.profile_name ? (
        <TemplateInsightsSection deviceId={device.id} rangeHours={range.hours} vendor={device.vendor || undefined} deviceType={device.device_type}
          groupFilter={hardwareOnly} section="hardware" title="Hardware health" onChart={onChart} initialGroup={initialGroup}
          emptyText="The monitoring template reports no hardware readings for this device (virtual appliances often expose none)." />
      ) : (
        <div className="rounded-xl border border-border bg-surface p-4 text-sm text-muted">No monitoring template is attached, so sensor, fan, power and stack readings are not collected. The component inventory below comes from ENTITY-MIB.</div>
      )}
      <EntityInventory entities={entities} loading={isPending} error={isError && !entities} />
    </section>
  )
}

function EntityInventory({ entities, loading, error }: { entities?: Entity[]; loading: boolean; error: boolean }) {
  const [query, setQuery] = useState('')
  const [cls, setCls] = useState('all')
  const classes = useMemo(() => {
    const counts = new Map<string, number>()
    for (const e of entities || []) counts.set(e.class || 'other', (counts.get(e.class || 'other') || 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [entities])
  const shown = (entities || []).filter((e) => (cls === 'all' || (e.class || 'other') === cls) &&
    (!query.trim() || [e.name, e.model_name, e.serial_number, e.class].join(' ').toLowerCase().includes(query.trim().toLowerCase())))
  return (
    <Panel title="Component inventory" meta={entities ? `${entities.length} component${entities.length === 1 ? '' : 's'} reported by ENTITY-MIB` : undefined}>
      {loading ? <p className="text-sm text-muted">Loading components…</p> : error ? <p className="text-sm text-warning">The component inventory could not be loaded.</p> : !entities?.length ? (
        <p className="text-sm text-muted">This device reports no ENTITY-MIB components.</p>
      ) : <>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name, model or serial…" aria-label="Search components"
            className="h-9 w-full rounded-lg border border-border bg-surface2 px-3 text-sm sm:w-72" />
          <div className="flex flex-wrap gap-1" role="group" aria-label="Component class">
            {[['all', entities.length] as [string, number], ...classes].map(([c, n]) => (
              <button key={c} type="button" aria-pressed={cls === c} onClick={() => setCls(c)}
                className={cn('rounded-full border px-2.5 py-0.5 text-xs', cls === c ? 'border-primary/50 bg-primary/10 text-primary' : 'border-border text-muted hover:text-text')}>
                {c === 'all' ? 'All' : c} · {n}
              </button>
            ))}
          </div>
        </div>
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-xs text-muted">
              <tr className="border-b border-border">
                {['Class', 'Name', 'Model', 'Serial', 'HW rev', 'FW rev'].map((h) => <th key={h} className="px-2 py-1.5 text-left font-medium">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {shown.map((e, i) => (
                <tr key={e.id || `${e.ent_index}-${i}`} className="border-b border-border/40 last:border-0">
                  <td className="px-2 py-1.5 text-muted">{e.class || MISSING}</td>
                  <td className="px-2 py-1.5 font-medium">{e.name || MISSING}</td>
                  <td className="px-2 py-1.5">{e.model_name || MISSING}</td>
                  <td className="px-2 py-1.5 font-mono text-[13px]">{e.serial_number || MISSING}</td>
                  <td className="px-2 py-1.5 text-muted">{e.hw_revision || MISSING}</td>
                  <td className="px-2 py-1.5 text-muted">{e.fw_revision || MISSING}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!shown.length && <p className="py-4 text-center text-sm text-muted">No components match.</p>}
        </div>
      </>}
    </Panel>
  )
}
