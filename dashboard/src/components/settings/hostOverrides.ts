/**
 * Manual host addresses for service checks (docs/service-check-connections.md): the shapes of
 * `/system/monitoring/host-overrides` and the form rules. The server validates again; these give the
 * same answers earlier, on the field they belong to.
 */
export type HostOverrideScope = 'all' | 'central' | 'sensor'

export interface HostOverrideEntry {
  id: string
  host: string
  address: string
  scope: HostOverrideScope
  sensor_id: string | null
  sensor_name: string | null
  sensor_missing: boolean
  note: string
  updated_at?: string | null
  updated_by?: string | null
  /** Running sensors this row reaches that are too old to apply it. */
  not_applied_by: string[]
}

export interface HostOverrideSensor {
  id: string
  name: string
  status: string
  version: string | null
  /** The sensor reported that it applies manual host addresses. */
  applies: boolean
}

export interface HostOverridesDoc {
  entries: HostOverrideEntry[]
  sensors: HostOverrideSensor[]
  max_entries: number
}

export interface HostOverrideForm {
  host: string
  address: string
  scope: HostOverrideScope
  sensor_id: string
  note: string
}

export type HostOverrideField = keyof HostOverrideForm
export type HostOverrideErrors = Partial<Record<HostOverrideField, string>>

export const EMPTY_HOST_OVERRIDE: HostOverrideForm = { host: '', address: '', scope: 'all', sensor_id: '', note: '' }
export const NOTE_MAX = 200

/** Where the two halves of "service check connections" live. */
export const CONNECTIONS_ANCHOR = 'service-check-connections'
export const PROBE_TRUST_ANCHOR = 'probe-trust'
export const CONNECTIONS_LINK = `/settings/general?tab=monitoring#${CONNECTIONS_ANCHOR}`
export const PROBE_TRUST_LINK = `/settings/general?tab=security#${PROBE_TRUST_ANCHOR}`
export const CONNECTIONS_PAGE = '/settings/general?tab=monitoring'
export const PROBE_TRUST_PAGE = '/settings/general?tab=security'

export function ipVersion(value: string): 4 | 6 | null {
  const text = value.trim().replace(/^\[(.*)\]$/, '$1')
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(text)) return text.split('.').every((part) => Number(part) <= 255) ? 4 : null
  if (!text.includes(':') || /[^0-9a-f:.]/i.test(text)) return null
  try {
    // The URL parser accepts exactly the IPv6 literals a connection can use.
    return new URL(`http://[${text}]/`).hostname ? 6 : null
  } catch {
    return null
  }
}

export function normalizeHostName(value: string): string {
  return value.trim().replace(/\.$/, '').toLowerCase()
}

function hostError(raw: string): string | undefined {
  const host = raw.trim()
  if (!host) return 'Enter the host name the checks use'
  if (host.includes('://') || host.includes('/')) return 'Enter the host name only, without http:// or a path'
  if (host.includes('*')) return 'Wildcards are not supported: enter each host name exactly'
  if (ipVersion(host)) return 'This is an address, not a name. A check of an address already connects to it'
  if (host.includes(':')) return 'Enter the host name only, without a port'
  const name = normalizeHostName(host)
  // Non-ASCII names are converted by the server; only the plain rules are checked here.
  if (/^[\x00-\x7f]*$/.test(name)) {
    const label = /^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$/
    if (!name || name.length > 253 || !name.split('.').every((part) => label.test(part))) return 'This is not a valid host name'
  }
  return undefined
}

function addressError(raw: string): string | undefined {
  const address = raw.trim()
  if (!address) return 'Enter the IPv4 or IPv6 address to connect to'
  if (address.includes('%')) return 'Addresses with an interface zone (%) are not supported'
  if (!ipVersion(address)) return 'Enter one IPv4 or IPv6 address, without a port, range or name'
  if (/^(0\.0\.0\.0|\[?::\]?)$/.test(address)) return 'This address cannot be connected to'
  return undefined
}

/** Field errors for the add/edit form; `editingId` is the row being changed, if any. */
export function validateHostOverride(form: HostOverrideForm, entries: readonly HostOverrideEntry[], editingId?: string | null): HostOverrideErrors {
  const errors: HostOverrideErrors = {}
  const host = hostError(form.host)
  if (host) errors.host = host
  const address = addressError(form.address)
  if (address) errors.address = address
  if (form.scope === 'sensor' && !form.sensor_id) errors.sensor_id = 'Choose the sensor this address applies to'
  if (form.note.trim().length > NOTE_MAX) errors.note = `Keep the note within ${NOTE_MAX} characters`
  if (!errors.host && !errors.sensor_id) {
    const name = normalizeHostName(form.host)
    const sensor = form.scope === 'sensor' ? form.sensor_id.toLowerCase() : ''
    const taken = entries.some((entry) => entry.id !== editingId && entry.host === name && entry.scope === form.scope
      && (entry.sensor_id || '').toLowerCase() === sensor)
    if (taken) errors.host = 'This name already has an address for that place. Edit that row instead'
  }
  return errors
}

export function formFromEntry(entry: HostOverrideEntry): HostOverrideForm {
  return { host: entry.host, address: entry.address, scope: entry.scope, sensor_id: entry.sensor_id || '', note: entry.note || '' }
}

export function payloadFromForm(form: HostOverrideForm) {
  return {
    host: form.host.trim(),
    address: form.address.trim(),
    scope: form.scope,
    sensor_id: form.scope === 'sensor' ? form.sensor_id : null,
    note: form.note.trim(),
  }
}

export function scopeLabel(entry: Pick<HostOverrideEntry, 'scope' | 'sensor_name' | 'sensor_missing'>): string {
  if (entry.scope === 'central') return 'This appliance only'
  if (entry.scope === 'sensor') return entry.sensor_missing ? 'A sensor that was removed' : `Sensor ${entry.sensor_name || ''}`.trim()
  return 'Everywhere'
}

/** Why a row does not take effect where it says it applies, or null. */
export function rowWarning(entry: Pick<HostOverrideEntry, 'scope' | 'sensor_missing' | 'not_applied_by'>): string | null {
  if (entry.sensor_missing) return 'The sensor this address was set for no longer exists, so it applies nowhere. Remove it or choose another place.'
  if (!entry.not_applied_by.length) return null
  const names = entry.not_applied_by.join(', ')
  return entry.scope === 'sensor'
    ? `${names} runs a version that cannot use manual addresses: it still looks the name up itself. Update the sensor.`
    : entry.not_applied_by.length === 1
      ? `Not applied by ${names}: that sensor runs a version that cannot use manual addresses and still looks the name up. Update it.`
      : `Not applied by ${names}: those sensors run versions that cannot use manual addresses and still look the name up. Update them.`
}

export interface ProbeTrustSummaryInput {
  auto_fetch_intermediates: boolean
  certificates: { is_ca: boolean; not_after: string }[]
}

/** One sentence for the Monitoring tab; the certificates themselves are managed on Security & TLS. */
export function trustSummary(trust: ProbeTrustSummaryInput, now: number = Date.now()): { text: string; expired: number } {
  const total = trust.certificates.length
  const authorities = trust.certificates.filter((cert) => cert.is_ca).length
  const expired = trust.certificates.filter((cert) => Date.parse(cert.not_after) < now).length
  const recovery = trust.auto_fetch_intermediates
    ? 'Missing intermediate certificates are fetched automatically.'
    : 'Missing intermediate certificates are not fetched automatically.'
  if (!total) return { text: `No additional certificates are trusted: checks accept certificates from public certificate authorities only. ${recovery}`, expired }
  const parts = [
    authorities ? `${authorities} certificate ${authorities === 1 ? 'authority' : 'authorities'}` : '',
    total - authorities ? `${total - authorities} self-signed service ${total - authorities === 1 ? 'certificate' : 'certificates'}` : '',
  ].filter(Boolean).join(' and ')
  return { text: `${parts} trusted in addition to the public certificate authorities. ${recovery}`, expired }
}
