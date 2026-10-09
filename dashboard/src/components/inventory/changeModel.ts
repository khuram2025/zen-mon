// Pure wording and links for change events (Documentation/Inventory/05-P1-API-CONTRACT.md section
// 3.7). Used by the Overview's recent-changes panel and the asset flyout; the Changes page can reuse
// it. No React, no router, no API: scripts/test-inventory-ui.mjs exercises it directly.

import type { ChangeRow } from '@/pages/inventory/types'

/** Subjects of OS, firmware and hardware events are internal column names; these are the words for them. */
const SUBJECT_LABELS: Record<string, string> = {
  os_version: 'OS version',
  os_name: 'Operating system',
  os_release: 'OS release',
  os_family: 'OS family',
  hw_serial: 'Serial number',
  hw_model: 'Hardware model',
  hw_vendor: 'Hardware vendor',
  hw_manufacturer: 'Hardware vendor',
  firmware_version: 'Firmware',
  serial_number: 'Serial number',
  model: 'Hardware model',
  vendor: 'Hardware vendor',
}

export const CATEGORY_LABELS: Record<ChangeRow['category'], string> = {
  software: 'Software',
  os: 'OS',
  firmware: 'Firmware',
  hardware: 'Hardware',
}

export function subjectLabel(subject: string): string {
  return SUBJECT_LABELS[subject] ?? subject
}

export interface ChangeText {
  /** What changed ("Google Chrome", "FortiOS", "Serial number"). */
  subject: string
  /** What happened to it ("upgraded", "installed", "removed", "changed", "downgraded"). */
  verb: string
  /** The values, when there are any ("151.0.7922.110 → 152.0.8001.12", "7.2.5"). */
  detail: string | null
  /** The event is the "(change budget exceeded)" marker, not a change of an asset. */
  marker: boolean
  /** One plain sentence for a screen reader and a tooltip. */
  sentence: string
}

const clean = (v: string | null | undefined): string | null => {
  const t = (v ?? '').trim()
  return t ? t : null
}

/**
 * The sentence for one event: "Google Chrome upgraded 151.0.7922.110 → 152.0.8001.12",
 * "FortiOS changed 7.2.5 → 7.2.8", "TeamViewer installed 15.5.3". A value that is missing is not
 * shown as a guess, and an upgrade is only called one when the server could order the versions
 * (`label`); otherwise the word is "changed".
 */
export function changeText(row: Pick<ChangeRow, 'category' | 'action' | 'subject' | 'old_value' | 'new_value' | 'label' | 'marker' | 'pair'>): ChangeText {
  const subject = row.category === 'software' ? row.subject : subjectLabel(row.subject)
  if (row.marker) {
    return {
      subject: 'Change budget exceeded',
      verb: '',
      detail: 'further changes of that report were not recorded',
      marker: true,
      sentence: 'Change budget exceeded: further changes of that report were not recorded',
    }
  }
  const oldV = clean(row.old_value)
  const newV = clean(row.new_value)
  let verb: string
  let detail: string | null
  if (row.action === 'added') {
    verb = row.category === 'software' ? 'installed' : 'added'
    detail = newV
  } else if (row.action === 'removed') {
    verb = 'removed'
    detail = oldV
  } else {
    verb = row.label === 'upgrade' ? 'upgraded' : row.label === 'downgrade' ? 'downgraded' : 'changed'
    detail = oldV && newV ? `${oldV} → ${newV}` : newV ? `to ${newV}` : oldV ? `from ${oldV}` : null
  }
  // Either half of a paired upgrade (a product whose name embeds its version is stored as a new row) says so.
  const paired = row.pair && row.action !== 'changed'
    ? ` (part of ${row.pair.label === 'upgrade' ? 'an upgrade' : row.pair.label === 'downgrade' ? 'a downgrade' : 'a version change'})`
    : ''
  return {
    subject,
    verb,
    detail,
    marker: false,
    sentence: `${subject} ${verb}${detail ? ` ${detail}` : ''}${paired}`,
  }
}

/**
 * Where an event's asset name goes. A deleted asset is plain text (history survives deletion); a
 * device that is a server's SNMP twin links to the server's flyout instead.
 */
export function changeAssetAddress(row: Pick<ChangeRow, 'asset_present' | 'asset_kind' | 'asset_id' | 'server_id'>): string | null {
  if (!row.asset_present) return null
  return row.server_id ? `server:${row.server_id}` : `${row.asset_kind}:${row.asset_id}`
}
