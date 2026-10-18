import type { DeviceRecord } from './types'

/* Tabs follow operator tasks, not data sources:
 *   Summary        is it healthy, and what is wrong?
 *   Performance    trends and capacity
 *   Interfaces     port-level troubleshooting (inside the device shell)
 *   <Vendor>       the template's domain groups (tunnels, SD-WAN, APs, pools…); a template
 *                  can declare its own tabs ("VPN & SD-WAN", "Wireless", "Pools & virtual
 *                  servers") and the default "<Vendor> metrics" tab keeps the rest
 *   Hardware       physical health: sensors, PSU, fans, stack, PoE, inventory
 *   Events         what happened, and when
 *   Configuration  how it is monitored and what it is
 * Old links keep working: ?tab=overview → summary, ?tab=inventory → configuration. */

export type BuiltinTabId = 'summary' | 'performance' | 'interfaces' | 'metrics' | 'hardware' | 'events' | 'configuration'
/** Template-declared vendor tabs ("VPN & SD-WAN" -> tpl-vpn-sd-wan); see templateTabId. */
export type TemplateTabId = `tpl-${string}`
export type DeviceTabId = BuiltinTabId | TemplateTabId

export type DeviceTab = { id: DeviceTabId; label: string; usesRange: boolean; groups?: string[] }

/** A vendor tab declared by the template (summary.template.tabs): its label and the groups it shows. */
export type TemplateTab = { id: string; label: string; groups: string[] }

/** What the shell knows about the template: its vendor, declared tabs and group digests. */
export type TemplateTabsInput = {
  vendor?: string | null
  tabs?: TemplateTab[] | null
  groups?: Array<{ key: string; kind?: string; tab?: string | null }> | null
}

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

/** URL id of a template tab; the same slug as the server's device_insights.tab_id. */
export function templateTabId(label: string): TemplateTabId {
  const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return `tpl-${slug || 'tab'}`
}

/** The tab that shows a template group: its declared vendor tab, else Hardware or the default vendor tab. */
export function placeGroup(group: { key: string; kind?: string; tab?: string | null }): DeviceTabId {
  if (group.tab) return templateTabId(group.tab)
  return isHardwareGroup(group) ? 'hardware' : 'metrics'
}

export function vendorTabLabel(device: Pick<DeviceRecord, 'vendor'>, templateVendor?: string | null): string {
  const vendor = (templateVendor || device.vendor || '').trim()
  return vendor && !/^(unknown|n\/a|not reported)$/i.test(vendor) ? `${vendor} metrics` : 'Device metrics'
}

export function deviceTabs(device: DeviceRecord, template?: TemplateTabsInput | string | null): DeviceTab[] {
  const info: TemplateTabsInput = typeof template === 'string' ? { vendor: template } : (template || {})
  const snmp = !!device.snmp_enabled
  const templated = snmp && !!device.profile_name
  const declared = templated ? (info.tabs || []).filter((t) => t.label && t.groups?.length) : []
  // The default vendor tab keeps every group without a declared tab. It is left out only once the summary
  // shows that the declared tabs (and Hardware) cover every group; before that it stays, so links work.
  const groups = info.groups
  const leftovers = !groups || groups.some((g) => placeGroup(g) === 'metrics')
  const showDefault = templated && (!declared.length || leftovers)
  return [
    { id: 'summary', label: 'Summary', usesRange: true },
    { id: 'performance', label: 'Performance', usesRange: true },
    ...(snmp ? [{ id: 'interfaces' as const, label: 'Interfaces', usesRange: true }] : []),
    ...(showDefault ? [{ id: 'metrics' as const, label: vendorTabLabel(device, info.vendor), usesRange: true }] : []),
    ...declared.map((t) => ({ id: templateTabId(t.label), label: t.label, usesRange: true, groups: t.groups })),
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
export function issueTab(issue: { tab: string; group?: string }, tabs: DeviceTab[],
  groupOf?: (key: string) => { kind?: string; tab?: string | null } | undefined): DeviceTabId {
  let id = issue.tab as DeviceTabId
  if (issue.group) id = placeGroup({ key: issue.group, ...groupOf?.(issue.group) })
  if (tabs.some((t) => t.id === id)) return id
  // A group whose own tab is not shown falls back to the default vendor tab, then Summary.
  return issue.group && tabs.some((t) => t.id === 'metrics') ? 'metrics' : 'summary'
}
