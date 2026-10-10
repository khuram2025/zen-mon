/**
 * Settings -> Storage: capacity and health, disks & volumes (add, claim,
 * allocate, remove), retention & cleanup, backup/restore and activity.
 *
 * Backend: /system/storage (layout + findings) and the /system/storage/*
 * management endpoints in server/app/api/v1/storage_management.py. Disk and
 * volume changes go through the root volume helper
 * (scripts/zenplus-volume-helper).
 */
import { useSearchParams } from 'react-router-dom'
import { Activity, Archive, Clock, HardDrive, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import { ActivitySection } from '@/components/storage/ActivitySection'
import { BackupsSection } from '@/components/storage/BackupsSection'
import { DisksSection } from '@/components/storage/DisksSection'
import { OverviewSection } from '@/components/storage/OverviewSection'
import { RetentionSection } from '@/components/storage/RetentionSection'
import type { StorageSection } from '@/components/storage/types'
import { useStorageStatus } from '@/components/storage/useStorage'

const SECTIONS: { id: StorageSection; label: string; icon: typeof HardDrive }[] = [
  { id: 'overview', label: 'Overview', icon: HardDrive },
  { id: 'disks', label: 'Disks & capacity', icon: Layers },
  { id: 'retention', label: 'Retention & cleanup', icon: Clock },
  { id: 'backups', label: 'Backups', icon: Archive },
  { id: 'activity', label: 'Activity', icon: Activity },
]

export function StorageTabContent() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('section') as StorageSection | null
  const active: StorageSection = SECTIONS.some((s) => s.id === requested) ? requested! : 'overview'
  const { data: status } = useStorageStatus(active === 'overview' || active === 'disks')

  // Count findings that need the operator, shown on the Disks tab.
  const diskAttention = (status?.issues || []).filter((i) =>
    i.action && ['claim', 'add', 'extend', 'remove_missing', 'rescan'].includes(i.action)).length

  function go(section: StorageSection) {
    const next = new URLSearchParams(params)
    if (section === 'overview') next.delete('section')
    else next.set('section', section)
    setParams(next, { replace: true })
  }

  return (
    <div className="space-y-4">
      <nav className="flex flex-wrap gap-1 border-b border-border" aria-label="Storage sections">
        {SECTIONS.map((s) => (
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
            <s.icon className="h-3.5 w-3.5" />
            {s.label}
            {s.id === 'disks' && diskAttention > 0 && (
              <span className="rounded-full bg-info/15 px-1.5 text-[10px] font-semibold tabular-nums text-info">
                {diskAttention}
              </span>
            )}
          </button>
        ))}
      </nav>

      {active === 'overview' && <OverviewSection onNavigate={go} />}
      {active === 'disks' && <DisksSection />}
      {active === 'retention' && <RetentionSection />}
      {active === 'backups' && <BackupsSection />}
      {active === 'activity' && <ActivitySection />}
    </div>
  )
}
