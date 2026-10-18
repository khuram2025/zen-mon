import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Box, Clock, GitBranch, HardDrive, Info, KeyRound, Layers, Save, Shield, Tag as TagIcon, Timer, Wifi } from 'lucide-react'
import { DeviceStatusExplanation } from '@/components/devices/DeviceStatusExplanation'
import { DeviceWidgetGrid } from '@/components/devices/DeviceWidgetGrid'
import { deviceOsLink } from '@/components/devices/deviceInventory'
import { autoDetectState } from '@/components/devices/templatePresentation'
import type { Insights } from '@/components/devices/TemplateInsightsSection'
import { MonitoringSites } from '@/components/MonitoringSites'
import { TagBadge } from '@/components/tags/TagBadge'
import { ConnectedEndpointsCard } from '@/components/udt/ConnectedEndpointsCard'
import { Card, CardContent } from '@/components/ui/Card'
import { useCan } from '@/stores/auth'
import { api } from '@/lib/api'
import { tagColor, tagColorMap, useTags } from '@/hooks/useTags'
import { inventoryApi } from '@/pages/inventory/api'
import { Panel } from './charts'
import { formatRelative, formatSpan } from './format'
import { OwnerNotesCard } from './OwnerNotesCard'
import type { DeviceRecord, DeviceSummary, IdentityField } from './types'

/* How the device is monitored and what it is: identity (one resolved answer
 * per field, with its source), polling and credentials status (never the
 * values), owner and notes, monitoring sites, the status policy explained, tags, configuration
 * backups and connected endpoints. */

const SOURCE: Record<string, string> = {
  inventory: 'from Inventory', device: 'reported by the device', template: 'from the template reading', entity: 'from ENTITY-MIB',
}

export function ConfigurationTab({ device, summary, onOpenHardware }: { device: DeviceRecord; summary?: DeviceSummary; onOpenHardware: () => void }) {
  const can = useCan()
  const { data: entities } = useQuery<any[]>({
    queryKey: ['device', device.id, 'entities'],
    queryFn: async () => (await api.get(`/devices/${device.id}/entities`)).data,
    enabled: !!device.snmp_enabled,
  })
  const snmp = !!device.snmp_enabled
  const widgets = [
    { id: 'inventory', title: 'Identity and monitoring', width: 6, height: 10, minWidth: 3, minHeight: 4,
      content: <InventoryConfigCard device={device} entities={entities || []} snmpHasData={undefined} identity={summary?.identity} onDetails={onOpenHardware} /> },
    { id: 'owner-notes', title: 'Owner and notes', width: 6, height: 7, minWidth: 3, minHeight: 4,
      content: <OwnerNotesCard device={device} /> },
    { id: 'monitoring-sites', title: 'Monitoring sites', width: 6, height: 7, minWidth: 2, minHeight: 3,
      content: <Panel title="Monitoring sites" meta="Where reachability is measured from"><MonitoringSites targetType="device" targetId={device.id} compact /></Panel> },
    { id: 'reachability', title: 'How status is decided', width: 12, height: 9, minWidth: 3, minHeight: 3,
      content: <Panel title="How status is decided" meta="The confirmation policy applied to this device"><DeviceStatusExplanation deviceId={device.id} /></Panel> },
    ...(can('ncm.view') ? [{ id: 'config-backups', title: 'Configuration backups', width: 6, height: 4, minWidth: 2, minHeight: 3,
      content: <Panel title="Configuration backups" meta="Network configuration management">
        <p className="text-sm text-muted">Backups, differences between versions and compliance for this device are kept in NCM.</p>
        <Link to={`/ncm/${device.id}`} className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"><Save className="h-3.5 w-3.5" />Open configuration history</Link>
      </Panel> }] : []),
    ...(snmp && ['switch', 'router', 'firewall'].includes(device.device_type) ? [{ id: 'connected-endpoints', title: 'Connected endpoints', width: 6, height: 9, minWidth: 3, minHeight: 4,
      content: <ConnectedEndpointsCard deviceId={device.id} /> }] : []),
  ]
  return (
    <section aria-label="Device configuration" className="space-y-3">
      <DeviceWidgetGrid deviceId={device.id} vendor={device.vendor || undefined} deviceType={device.device_type} section="inventory" widgets={widgets} />
    </section>
  )
}

/** Identity and how the device is monitored. With `identity` (the summary API's
 *  resolved answer) every field shows the value the header and every tab show. */
export function InventoryConfigCard({
  device, entities, snmpHasData, onDetails, identity,
}: { device: any; entities: any[]; snmpHasData?: boolean; onDetails: () => void; identity?: DeviceSummary['identity'] }) {
  const { data: templateInsights } = useQuery<Insights>({
    queryKey: ['device', device.id, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${device.id}/template-insights`)).data,
    enabled: !!device.snmp_enabled && !identity,
    staleTime: 30_000,
  })
  const templateText = (names: string[]) => templateInsights?.groups.flatMap(group => group.metrics || [])
    .find(metric => metric.has_data && metric.type === 'string' && names.includes(metric.name.toLowerCase()) && metric.text)?.text
  const snmp = !!device.snmp_enabled
  // ENTITY-MIB chassis row — fallback when the device record has no serial/firmware yet.
  const chassis = entities.find((e) => e.class === 'chassis')
  // Inventory & Compliance: the OS / firmware value links to the network devices on the same release. The
  // family and release are the normalised ones (inventory_identity), so they come from Inventory's own
  // asset endpoint; without inventory.view nothing is requested and the value stays text. An answer of
  // 404 (an SNMP twin of a server, or out of scope) or any failure also leaves plain text.
  const canInventory = useCan()('inventory.view')
  const { data: invAsset } = useQuery({
    queryKey: ['inventory', 'asset', `device:${device.id}`],
    queryFn: () => inventoryApi.assetDetail(`device:${device.id}`),
    enabled: canInventory && !!device.id,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const osLink = canInventory ? deviceOsLink(invAsset?.asset) : null
  const inventoryValue = (row: 'os' | 'firmware', text: string, reported: boolean): React.ReactNode =>
    osLink && osLink.row === row && reported ? (
      <Link
        to={osLink.href}
        className="text-primary hover:underline"
        title={`Open the network devices running ${osLink.release} in Inventory`}
      >
        {text}
      </Link>
    ) : text
  const auto = autoDetectState(snmpHasData)
  const { data: tagDefs } = useTags()
  const tagColors = useMemo(() => tagColorMap(tagDefs), [tagDefs])
  const NOT_REPORTED = 'Not reported'
  const resolved = (field?: IdentityField) => field?.value ? { text: field.value, source: field.source ? SOURCE[field.source] : null } : null
  const osResolved = resolved(identity?.os_version)
  const fwResolved = resolved(identity?.firmware)
  const serialResolved = resolved(identity?.serial_number)
  const modelResolved = resolved(identity?.model)
  const osVersionText: string = osResolved?.text || device.os_version || templateText(['tmos version', 'os version', 'software version', 'firmware']) || NOT_REPORTED
  const firmwareText: string = fwResolved?.text || device.firmware_version || chassis?.fw_revision || templateText(['firmware version']) || NOT_REPORTED
  const withSource = (text: React.ReactNode, source?: string | null) => source
    ? <>{text}<span className="block text-xs font-normal text-muted">{source}</span></>
    : text
  const credential = !snmp ? 'SNMP disabled'
    : device.snmp_credential_id ? 'Saved credential profile'
      : String(device.snmp_version) === '3'
        ? `SNMPv3 · authentication ${device.snmp_auth_configured ? 'set' : 'not set'} · privacy ${device.snmp_priv_configured ? 'set' : 'not set'}`
        : 'Community string set on the device'
  const rows: Array<{ icon: React.ComponentType<{ className?: string }>; label: string; value: React.ReactNode }> = [
    { icon: HardDrive, label: 'Serial number', value: withSource(serialResolved?.text || device.serial_number || chassis?.serial_number || templateText(['chassis serial', 'serial number', 'system serial number']) || 'Not reported', serialResolved?.source) },
    ...((modelResolved?.text || (!device.model && chassis?.model_name)) ? [{ icon: Box, label: 'Model', value: withSource(modelResolved?.text || chassis.model_name, modelResolved?.source) }] : []),
    { icon: GitBranch, label: 'OS / version', value: withSource(inventoryValue('os', osVersionText, osVersionText !== NOT_REPORTED), osResolved?.source) },
    { icon: GitBranch, label: 'Firmware', value: withSource(inventoryValue('firmware', firmwareText, firmwareText !== NOT_REPORTED), fwResolved?.source) },
    { icon: Shield, label: 'SNMP', value: snmp ? `v${device.snmp_version} · port ${device.snmp_port}` : 'Disabled' },
    ...(snmp ? [{ icon: KeyRound, label: 'Credentials', value: credential }] : []),
    {
      icon: Layers, label: 'Monitoring Template',
      value: device.profile_name
        ? <Link to="/settings/general?tab=templates" className="text-primary hover:underline">{device.profile_name}</Link>
        : snmp
          ? <span>{auto.label}{auto.reason && <span className="block text-xs font-normal text-muted">{auto.reason}</span>}</span>
          : '—',
    },
    { icon: Wifi, label: 'Ping', value: device.ping_enabled ? `Enabled · ${device.ping_interval}s` : 'Disabled' },
    ...(snmp && device.snmp_poll_interval ? [{ icon: Timer, label: 'SNMP polling', value: `Every ${formatSpan(Number(device.snmp_poll_interval))}` }] : []),
    { icon: Info, label: 'System OID', value: <span className="break-all font-mono text-[13px]">{device.sys_object_id || '—'}</span> },
    { icon: Layers, label: 'Hardware', value: entities.length > 0 ? <button type="button" onClick={onDetails} className="text-primary hover:underline">{entities.length} component{entities.length === 1 ? '' : 's'}</button> : '—' },
    { icon: Clock, label: 'Last Updated', value: <span title={device.updated_at || device.last_seen || undefined}>{formatRelative(device.updated_at || device.last_seen)}</span> },
  ]
  const tags: string[] = Array.isArray(device.tags) ? device.tags : []

  return (
    <Card className="h-full">
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Identity &amp; monitoring</h3>
        </div>
        <div className="space-y-1.5 text-sm">
          {rows.map((r, i) => (
            <div key={i} className="flex items-start justify-between gap-3 border-b border-border/40 pb-1.5 last:border-0">
              <span className="flex shrink-0 items-center gap-2 text-muted">
                <r.icon className="h-3.5 w-3.5" />
                {r.label}
              </span>
              <span className="min-w-0 break-words text-right font-medium">{r.value}</span>
            </div>
          ))}
          <div className="flex items-start justify-between gap-3 pt-1">
            <span className="flex items-center gap-2 text-muted">
              <TagIcon className="h-3.5 w-3.5" />
              Tags
            </span>
            <div className="flex flex-wrap justify-end gap-1">
              {tags.length === 0 ? (
                <span className="text-muted">—</span>
              ) : (
                tags.map((t) => (
                  <Link key={t} to={`/devices?tag=${encodeURIComponent(t)}`} aria-label={`Show devices tagged ${t}`}>
                    <TagBadge name={t} color={tagColor(t, tagColors)} />
                  </Link>
                ))
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
