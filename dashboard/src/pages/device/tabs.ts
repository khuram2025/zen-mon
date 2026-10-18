import type { DeviceRecord } from './types'

/* Tabs follow operator tasks, not data sources:
 *   Summary        is it healthy, and what is wrong?
 *   Performance    trends and capacity
 *   Interfaces     port-level troubleshooting (inside the device shell)
 *   <Vendor>       the template's domain groups (tunnels, SD-WAN, APs, pools…)
 *   Hardware       physical health: sensors, PSU, fans, stack, PoE, inventory
 *   Events         what happened, and when
 *   Configuration  how it is monitored and what it is
 * Old links keep working: ?tab=overview → summary, ?tab=inventory → configuration. */

export type DeviceTabId = 'summary' | 'performance' | 'interfaces' | 'metrics' | 'hardware' | 'events' | 'configuration'

export type DeviceTab = { id: DeviceTabId; label: string; usesRange: boolean }

const LEGACY: Record<string, DeviceTabId> = { overview: 'summary', inventory: 'configuration' }

// Physical-health groups across the built-in templates; everything else is the vendor tab's.
const HARDWARE_KEYS = new Set([
  'fans', 'psu', 'psus', 'temperature', 'sensors', 'health', 'alarms', 'stack', 'stack_members', 'vsf_members',
  'poe', 'memory64', 'disks', 'optics', 'system_modules', 'redundancy',
])

export function isHardwareGroup(group: { key: string; kind?: string }): boolean {
  // "memory" is a table of memory pools on switches (hardware) but the F5
  // system/TMM/non-TMM/swap scalar group elsewhere (vendor metrics).
  if (group.key === 'memory') return group.kind === 'table'
  return HARDWARE_KEYS.has(group.key)
}

export function vendorTabLabel(device: Pick<DeviceRecord, 'vendor'>, templateVendor?: string | null): string {
  const vendor = (templateVendor || device.vendor || '').trim()
  return vendor && !/^(unknown|n\/a|not reported)$/i.test(vendor) ? `${vendor} metrics` : 'Device metrics'
}

export function deviceTabs(device: DeviceRecord, templateVendor?: string | null): DeviceTab[] {
  const snmp = !!device.snmp_enabled
  return [
    { id: 'summary', label: 'Summary', usesRange: true },
    { id: 'performance', label: 'Performance', usesRange: true },
    ...(snmp ? [{ id: 'interfaces' as const, label: 'Interfaces', usesRange: true }] : []),
    ...(snmp && device.profile_name ? [{ id: 'metrics' as const, label: vendorTabLabel(device, templateVendor), usesRange: true }] : []),
    ...(snmp ? [{ id: 'hardware' as const, label: 'Hardware', usesRange: true }] : []),
    { id: 'events', label: 'Events', usesRange: true },
    { id: 'configuration', label: 'Configuration', usesRange: false },
  ]
}

export function resolveTab(requested: string | null, tabs: DeviceTab[]): DeviceTabId {
  const id = (requested && LEGACY[requested]) || requested
  return (tabs.find((t) => t.id === id)?.id) || 'summary'
}

/** Where an issue opens: template groups go to the tab that shows them. */
export function issueTab(issue: { tab: string; group?: string }, tabs: DeviceTab[], kindOf?: (key: string) => string | undefined): DeviceTabId {
  let id = issue.tab as DeviceTabId
  if (issue.group) id = isHardwareGroup({ key: issue.group, kind: kindOf?.(issue.group) }) ? 'hardware' : 'metrics'
  return tabs.some((t) => t.id === id) ? id : 'summary'
}
