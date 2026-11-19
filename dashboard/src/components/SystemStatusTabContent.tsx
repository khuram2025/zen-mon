/**
 * Settings -> System status (the tab that used to be "Storage"): the health of the appliance itself
 * (CPU, memory, services, data stores, software) and its storage: capacity, disks & volumes (add, claim,
 * allocate, remove), retention & cleanup, backup/restore and activity.
 *
 * The tab keeps its address, `?tab=storage`: role page lists and the links in sent reports refer to it.
 * Without `section` it opens the health overview; `section=storage` is the storage overview and the
 * other storage sections keep their names.
 *
 * Backend: /system/status (server/app/services/appliance_health.py), /system/storage (layout + findings)
 * and the /system/storage/* management endpoints in server/app/api/v1/storage_management.py. Disk and
 * volume changes go through the root volume helper (scripts/zenplus-volume-helper).
 */
import { useSearchParams } from 'react-router-dom'
import { Activity, Archive, Clock, Gauge, HardDrive, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import { ActivitySection } from '@/components/storage/ActivitySection'
import { BackupsSection } from '@/components/storage/BackupsSection'
import { DisksSection } from '@/components/storage/DisksSection'
import { OverviewSection } from '@/components/storage/OverviewSection'
import { RetentionSection } from '@/components/storage/RetentionSection'
import type { StorageSection } from '@/components/storage/types'
import { useStorageStatus } from '@/components/storage/useStorage'
import { HealthSection } from '@/components/system/HealthSection'
import { SYSTEM_SECTIONS, sectionFromParam, sectionParam, type SystemSection } from '@/components/system/sections'

const ICONS: Record<SystemSection, typeof HardDrive> = {
  health: Gauge, storage: HardDrive, disks: Layers, retention: Clock, backups: Archive, activity: Activity,
}

export function SystemStatusTabContent() {
  const [params, setParams] = useSearchParams()
  const active = sectionFromParam(params.get('section'))
  // The health overview shows storage findings too, so it reads the same status as the storage sections.
  const { data: status } = useStorageStatus(active === 'health' || active === 'storage' || active === 'disks')

  // Count findings that need the operator, shown on the Disks tab.
  const diskAttention = (status?.issues || []).filter((i) =>
    i.action && ['claim', 'add', 'extend', 'remove_missing', 'rescan'].includes(i.action)).length

  function go(section: SystemSection) {
    const next = new URLSearchParams(params)
    const value = sectionParam(section)
    if (value) next.set('section', value)
    else next.delete('section')
    setParams(next, { replace: true })
  }
  // The storage sections name their own overview "overview".
  const goStorage = (section: StorageSection) => go(section === 'overview' ? 'storage' : section)

  return (
    <div className="space-y-4">
      <nav className="flex flex-wrap gap-1 border-b border-border" aria-label="System status sections">
        {SYSTEM_SECTIONS.map((s) => {
          const Icon = ICONS[s.id]
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => go(s.id)}
              aria-current={active === s.id ? 'page' : undefined}
              className={cn(
                '-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                active === s.id
                  ? 'border-primary text-text'
                  : 'border-transparent text-text2 hover:border-border hover:text-text',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {s.label}
              {s.id === 'disks' && diskAttention > 0 && (
                <span className="rounded-full bg-info/15 px-1.5 text-2xs font-semibold tabular-nums text-info">
                  {diskAttention}
                </span>
              )}
            </button>
          )
        })}
      </nav>

      {active === 'health' && <HealthSection onNavigate={go} />}
      {active === 'storage' && <OverviewSection onNavigate={goStorage} />}
      {active === 'disks' && <DisksSection />}
      {active === 'retention' && <RetentionSection />}
      {active === 'backups' && <BackupsSection />}
      {active === 'activity' && <ActivitySection />}
    </div>
  )
}
