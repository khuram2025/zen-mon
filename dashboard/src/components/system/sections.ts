// The sections of Settings -> System status and their `?section=` values. Self-contained so
// scripts/test-system-status.mjs can exercise the old-link rules without React.

export type SystemSection = 'health' | 'storage' | 'disks' | 'retention' | 'backups' | 'activity'

export const SYSTEM_SECTIONS: { id: SystemSection; label: string }[] = [
  { id: 'health', label: 'Overview' },
  { id: 'storage', label: 'Storage' },
  { id: 'disks', label: 'Disks & capacity' },
  { id: 'retention', label: 'Retention & cleanup' },
  { id: 'backups', label: 'Backups' },
  { id: 'activity', label: 'Activity' },
]

/** The tab's address. It is still `storage`: role page lists and links in sent reports refer to it. */
export const SYSTEM_STATUS_TAB = 'storage'
/** Other spellings of the tab that open it (typed by hand, or a later rename of the address). */
export const SYSTEM_STATUS_TAB_ALIASES: readonly string[] = ['system', 'system-status', 'status', 'health']

/** `?tab=` as the tab it opens: the aliases of System status become its address, everything else is unchanged. */
export function canonicalTab(tab: string | null): string | null {
  return tab && SYSTEM_STATUS_TAB_ALIASES.includes(tab) ? SYSTEM_STATUS_TAB : tab
}

/**
 * `?section=` as a section. No value opens the health overview. `overview` was never written into an address
 * (the storage overview was the default), but it is accepted for the storage overview in case it was typed.
 */
export function sectionFromParam(value: string | null): SystemSection {
  if (value === 'overview') return 'storage'
  return SYSTEM_SECTIONS.some((s) => s.id === value) ? (value as SystemSection) : 'health'
}

/** The `?section=` value of a section; null for the default one (no parameter). */
export function sectionParam(section: SystemSection): string | null {
  return section === 'health' ? null : section
}

/** Address of a section, for links from other pages. */
export function systemStatusHref(section: SystemSection = 'health'): string {
  const value = sectionParam(section)
  return `/settings/general?tab=${SYSTEM_STATUS_TAB}${value ? `&section=${value}` : ''}`
}
