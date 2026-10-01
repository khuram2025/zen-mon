/**
 * Links to one flow interface from outside the NetFlow explorer (X-19) and from breakdowns to the
 * one utilization definition (Capacity).
 *
 * A NetFlow interface is an exporter plus an ifIndex: ifIndex 1 exists on every exporter, so a
 * link names the exporter as `iface=N@exporter`. That exporter is the one whose flow records
 * crossed the interface — not necessarily the device's management address, which need not export
 * flows at all (a device mapped to exporter 198.18.220.1 while managed on 10.220.250.1).
 */
import type { TimeRange } from '@/components/TimeRangePicker'
import type { FlowFilter } from './filters'
import { buildNetflowLink } from './links'
import { nfWindowFromRange, type NfWindow } from './window'

/** `N@exporter`: the qualified interface filter value. */
export function qualifiedIface(ifindex: number, exporter: string): string {
  return `${ifindex}@${exporter}`
}

/**
 * The Capacity page narrowed to one interface (`iface` = `N@exporter`), in the given window:
 * utilization is measured there (per direction, complete 5-minute samples, exporter gaps left
 * out, duplicate ingress/egress export counted once), never recomputed by a breakdown.
 */
export function capacityLink(iface: string, window?: NfWindow | null): string {
  const filter: FlowFilter = { iface }
  return buildNetflowLink('/netflow/capacity', { filter, window: window ?? null })
}

export interface InterfaceExporter {
  exporter: string
  bytes: number
}

export interface NetflowInterfaceLink {
  exporter: string
  /** Overview of this interface on this exporter, in the source page's window. */
  to: string
  bytes: number
}

/**
 * Explorer links for a device interface seen from Link Utilization: one per exporter whose flows
 * crossed this ifIndex in the window (busiest first), each as `iface=N@exporter` with the Link
 * Utilization window (preset or custom bounds). When the window had no flows on the interface,
 * the device's flow exporters are offered (`fallbackExporters`), still qualified.
 */
export function netflowInterfaceLinks(
  ifindex: number,
  interfaceExporters: InterfaceExporter[] | null | undefined,
  range: TimeRange | NfWindow,
  fallbackExporters?: string[] | null,
): NetflowInterfaceLink[] {
  const window = 'mode' in range ? range : nfWindowFromRange(range)
  const seen = (interfaceExporters ?? []).filter((e) => e && e.exporter)
  const list: InterfaceExporter[] = seen.length
    ? [...seen].sort((a, b) => b.bytes - a.bytes || a.exporter.localeCompare(b.exporter))
    : (fallbackExporters ?? []).map((exporter) => ({ exporter, bytes: 0 }))
  return list.map((e) => ({
    exporter: e.exporter,
    bytes: e.bytes,
    to: buildNetflowLink('/netflow', { filter: { iface: qualifiedIface(ifindex, e.exporter) }, window }),
  }))
}
