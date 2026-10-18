/* GET /devices/{id}/related, and the wording the Related panel uses for it. Kept free of React so
 * scripts/test-device-page-phase3.mjs can check it. */

export type RelatedDevice = { id: string; hostname: string; ip_address: string | null; device_type: string; status: string }

export type SameTimeMatch = {
  at: string
  old_status: string
  new_status: string
  /** Seconds from this device's nearest transition: negative = before it. */
  offset_s: number
  anchor: { at: string; old_status: string; new_status: string }
}

export type DeviceRelated = {
  device_id: string
  parent: RelatedDevice | null
  children: { total: number; by_status: Record<string, number>; items: RelatedDevice[] }
  dependencies: {
    upstream: Array<RelatedDevice & { dependency_type: string; suppress_alerts: boolean; enabled: boolean }>
    downstream: Array<RelatedDevice & { dependency_type: string; suppress_alerts: boolean; enabled: boolean }>
  }
  /** null without maps.view. */
  maps: { maps: Array<{ id: string; name: string }>; neighbours: RelatedDevice[] } | null
  /** null when the status log is unavailable (named in `unavailable`). */
  same_time: {
    window_s: number
    transitions: Array<{ at: string; old_status: string; new_status: string }>
    devices: Array<RelatedDevice & { matches: SameTimeMatch[]; shared_transitions: number; closest_s: number }>
  } | null
  unavailable: string[]
}

const STATUS_WORD: Record<string, string> = { up: 'Up', down: 'Down', degraded: 'Degraded', unknown: 'Unknown', maintenance: 'Maintenance' }

export function statusWord(status: string | null | undefined): string {
  const s = (status || '').toLowerCase()
  return STATUS_WORD[s] || (s ? s[0].toUpperCase() + s.slice(1) : 'Unknown')
}

/** "40 s after", "1 min 10 s before", "at the same moment". */
export function offsetText(offsetS: number): string {
  const abs = Math.abs(Math.round(offsetS))
  if (abs === 0) return 'at the same moment'
  const span = abs >= 60 ? `${Math.floor(abs / 60)} min${abs % 60 ? ` ${abs % 60} s` : ''}` : `${abs} s`
  return `${span} ${offsetS < 0 ? 'before' : 'after'}`
}

/** One line for a same-time match: "Down 40 s after this device went Down". */
export function sameTimeText(match: SameTimeMatch): string {
  return `${statusWord(match.new_status)} ${offsetText(match.offset_s)} this device went ${statusWord(match.anchor.new_status)}`
}

/** "12 managed · 3 down · 1 degraded" (worst states named, up implied by the total). */
export function childrenText(children: DeviceRelated['children']): string {
  const parts = [`${children.total.toLocaleString()} managed`]
  for (const s of ['down', 'degraded', 'unknown', 'maintenance']) {
    const n = children.by_status[s]
    if (n) parts.push(`${n.toLocaleString()} ${statusWord(s).toLowerCase()}`)
  }
  return parts.join(' · ')
}

const DEPENDENCY_LABEL: Record<string, string> = {
  uplink: 'Uplink', wan: 'WAN', power: 'Power', site: 'Site', service: 'Service', manual: 'Manual',
}
export function dependencyLabel(kind: string): string {
  return DEPENDENCY_LABEL[kind] || kind
}

/** The network map deep link that opens the map in live mode, centred on this device. */
export function mapFocusHref(mapId: string, deviceId: string): string {
  return `/maps/manual?map=${encodeURIComponent(mapId)}&mode=live&focus=${encodeURIComponent(deviceId)}`
}

/** Nothing to show at all (the panel then says so instead of drawing empty sections). */
export function relatedIsEmpty(r: DeviceRelated): boolean {
  return !r.parent && !r.children.total && !r.dependencies.upstream.length && !r.dependencies.downstream.length
    && !(r.maps?.maps.length) && !(r.same_time?.devices.length)
}
